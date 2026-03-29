// SPDX-License-Identifier: GPL-2.0-only
/*
 * MNT Pocket Reform RP2040 sysctl transport
 *
 * Owns the SPI connection to the RP2040 system controller and exposes
 * typed helpers for other in-kernel Pocket Reform users.
 */

#include <linux/delay.h>
#include <linux/export.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/mnt-pocket-reform-sysctl.h>
#include <linux/of.h>
#include <linux/spi/spi.h>

#define MNT_SYSCTL_SPI_MAGIC			0xb5
#define MNT_SYSCTL_SPI_REQ_LEN			4
#define MNT_SYSCTL_SPI_RESP_LEN			8
#define MNT_SYSCTL_SPI_MAX_SPEED_HZ		4000000
#define MNT_SYSCTL_SPI_XFER_RETRIES		5

#define MNT_SYSCTL_SPI_STATUS_OK		0
#define MNT_SYSCTL_SPI_STATUS_ERR_UNSUPPORTED	1
#define MNT_SYSCTL_SPI_STATUS_ERR_INVALID_ARG	2
#define MNT_SYSCTL_SPI_STATUS_ERR_INVALID_STATE	3
#define MNT_SYSCTL_SPI_STATUS_ERR_INTERNAL	4

#define MNT_SYSCTL_CMD_GET_DISPLAY_ABI		'A'
#define MNT_SYSCTL_CMD_SET_BACKLIGHT		'b'
#define MNT_SYSCTL_CMD_SET_DISP_EN		'E'
#define MNT_SYSCTL_CMD_GET_DISP_EN		'e'
#define MNT_SYSCTL_CMD_SET_DISP_RESET		'R'
#define MNT_SYSCTL_CMD_GET_DISP_RESET		'r'

struct mnt_pocket_reform_sysctl {
	struct spi_device *spi;
	struct mutex lock;
};

static DEFINE_MUTEX(mnt_pocket_reform_sysctl_dev_lock);
static struct mnt_pocket_reform_sysctl *mnt_pocket_reform_sysctl_dev;

static u8 mnt_pocket_reform_sysctl_checksum(const u8 *buf, size_t len)
{
	u8 checksum = 0;
	size_t i;

	for (i = 0; i < len - 1; i++)
		checksum ^= buf[i];

	return checksum;
}

static struct mnt_pocket_reform_sysctl *mnt_pocket_reform_sysctl_get_dev(void)
{
	struct mnt_pocket_reform_sysctl *sysctl;

	mutex_lock(&mnt_pocket_reform_sysctl_dev_lock);
	sysctl = mnt_pocket_reform_sysctl_dev;
	mutex_unlock(&mnt_pocket_reform_sysctl_dev_lock);

	return sysctl;
}

static int mnt_pocket_reform_sysctl_xfer(u8 cmd, u8 arg, u8 *resp)
{
	struct mnt_pocket_reform_sysctl *sysctl;
	u8 req[MNT_SYSCTL_SPI_REQ_LEN] = {
		MNT_SYSCTL_SPI_MAGIC,
		cmd,
		arg,
		0,
	};
	int ret;

	sysctl = mnt_pocket_reform_sysctl_get_dev();
	if (!sysctl)
		return -EPROBE_DEFER;

	req[3] = mnt_pocket_reform_sysctl_checksum(req, sizeof(req));

	mutex_lock(&sysctl->lock);
	usleep_range(2000, 3000);
	ret = spi_write(sysctl->spi, req, sizeof(req));
	if (!ret) {
		usleep_range(3000, 4000);
		ret = spi_read(sysctl->spi, resp, MNT_SYSCTL_SPI_RESP_LEN);
	}
	mutex_unlock(&sysctl->lock);

	return ret;
}

static bool mnt_pocket_reform_sysctl_zero_reply(const u8 *resp)
{
	int i;

	for (i = 0; i < MNT_SYSCTL_SPI_RESP_LEN; i++) {
		if (resp[i] != 0)
			return false;
	}

	return true;
}

static int mnt_pocket_reform_sysctl_check_status(u8 status, u8 cmd)
{
	switch (status) {
	case MNT_SYSCTL_SPI_STATUS_OK:
		return 0;
	case MNT_SYSCTL_SPI_STATUS_ERR_UNSUPPORTED:
		return -ENODEV;
	case MNT_SYSCTL_SPI_STATUS_ERR_INVALID_ARG:
		return -EINVAL;
	case MNT_SYSCTL_SPI_STATUS_ERR_INVALID_STATE:
	case MNT_SYSCTL_SPI_STATUS_ERR_INTERNAL:
		return -EIO;
	default:
		pr_err("mnt-pocket-reform-sysctl: command 0x%02x returned invalid status %u\n",
		       cmd, status);
		return -EPROTO;
	}
}

int mnt_pocket_reform_sysctl_get_display_abi(u8 *version, u8 *caps)
{
	u8 resp[MNT_SYSCTL_SPI_RESP_LEN];
	int ret;
	int attempt;

	for (attempt = 0; attempt < MNT_SYSCTL_SPI_XFER_RETRIES; attempt++) {
		memset(resp, 0, sizeof(resp));

		ret = mnt_pocket_reform_sysctl_xfer(MNT_SYSCTL_CMD_GET_DISPLAY_ABI,
						    0, resp);
		if (ret < 0)
			return ret;

		if (mnt_pocket_reform_sysctl_zero_reply(resp) ||
		    (resp[0] == MNT_SYSCTL_SPI_STATUS_OK && resp[1] == 0)) {
			usleep_range(3000, 5000);
			continue;
		}

		ret = mnt_pocket_reform_sysctl_check_status(resp[0],
							    MNT_SYSCTL_CMD_GET_DISPLAY_ABI);
		if (ret < 0)
			return ret;

		if (version)
			*version = resp[1];
		if (caps)
			*caps = resp[2];

		return 0;
	}

	return -EAGAIN;
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_get_display_abi);

static int mnt_pocket_reform_sysctl_set_bool_cmd(u8 cmd, bool value)
{
	u8 resp[MNT_SYSCTL_SPI_RESP_LEN] = { 0 };
	int ret;

	ret = mnt_pocket_reform_sysctl_xfer(cmd, value ? 1 : 0, resp);
	if (ret < 0)
		return ret;

	return mnt_pocket_reform_sysctl_check_status(resp[0], cmd);
}

static int mnt_pocket_reform_sysctl_get_bool_cmd(u8 cmd, bool *value)
{
	u8 resp[MNT_SYSCTL_SPI_RESP_LEN] = { 0 };
	int ret;

	ret = mnt_pocket_reform_sysctl_xfer(cmd, 0, resp);
	if (ret < 0)
		return ret;

	ret = mnt_pocket_reform_sysctl_check_status(resp[0], cmd);
	if (ret < 0)
		return ret;

	*value = !!resp[1];
	return 0;
}

int mnt_pocket_reform_sysctl_set_disp_en(bool enabled)
{
	return mnt_pocket_reform_sysctl_set_bool_cmd(MNT_SYSCTL_CMD_SET_DISP_EN,
							 enabled);
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_set_disp_en);

int mnt_pocket_reform_sysctl_get_disp_en(bool *enabled)
{
	return mnt_pocket_reform_sysctl_get_bool_cmd(MNT_SYSCTL_CMD_GET_DISP_EN,
							 enabled);
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_get_disp_en);

int mnt_pocket_reform_sysctl_set_disp_reset(bool asserted)
{
	return mnt_pocket_reform_sysctl_set_bool_cmd(MNT_SYSCTL_CMD_SET_DISP_RESET,
							 asserted);
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_set_disp_reset);

int mnt_pocket_reform_sysctl_get_disp_reset(bool *asserted)
{
	return mnt_pocket_reform_sysctl_get_bool_cmd(MNT_SYSCTL_CMD_GET_DISP_RESET,
							 asserted);
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_get_disp_reset);

int mnt_pocket_reform_sysctl_set_backlight_percent(u8 percent)
{
	u8 resp[MNT_SYSCTL_SPI_RESP_LEN] = { 0 };
	int ret;

	ret = mnt_pocket_reform_sysctl_xfer(MNT_SYSCTL_CMD_SET_BACKLIGHT,
					    percent, resp);
	if (ret < 0)
		return ret;

	return mnt_pocket_reform_sysctl_check_status(resp[0],
						     MNT_SYSCTL_CMD_SET_BACKLIGHT);
}
EXPORT_SYMBOL_GPL(mnt_pocket_reform_sysctl_set_backlight_percent);

static int mnt_pocket_reform_sysctl_probe(struct spi_device *spi)
{
	struct mnt_pocket_reform_sysctl *sysctl;
	int ret;

	sysctl = devm_kzalloc(&spi->dev, sizeof(*sysctl), GFP_KERNEL);
	if (!sysctl)
		return -ENOMEM;

	spi->max_speed_hz = MNT_SYSCTL_SPI_MAX_SPEED_HZ;
	spi->mode = SPI_MODE_1;
	spi->bits_per_word = 8;
	ret = spi_setup(spi);
	if (ret < 0)
		return dev_err_probe(&spi->dev, ret,
				     "failed to configure SPI device\n");

	sysctl->spi = spi;
	mutex_init(&sysctl->lock);
	spi_set_drvdata(spi, sysctl);

	mutex_lock(&mnt_pocket_reform_sysctl_dev_lock);
	if (mnt_pocket_reform_sysctl_dev) {
		mutex_unlock(&mnt_pocket_reform_sysctl_dev_lock);
		return dev_err_probe(&spi->dev, -EBUSY,
				     "sysctl transport already registered\n");
	}

	mnt_pocket_reform_sysctl_dev = sysctl;
	mutex_unlock(&mnt_pocket_reform_sysctl_dev_lock);

	dev_info(&spi->dev, "Pocket Reform sysctl transport ready\n");

	return 0;
}

static void mnt_pocket_reform_sysctl_remove(struct spi_device *spi)
{
	struct mnt_pocket_reform_sysctl *sysctl = spi_get_drvdata(spi);

	mutex_lock(&mnt_pocket_reform_sysctl_dev_lock);
	if (mnt_pocket_reform_sysctl_dev == sysctl)
		mnt_pocket_reform_sysctl_dev = NULL;
	mutex_unlock(&mnt_pocket_reform_sysctl_dev_lock);
}

static const struct of_device_id mnt_pocket_reform_sysctl_of_match[] = {
	{ .compatible = "mntre,lpc11u24" },
	{ }
};
MODULE_DEVICE_TABLE(of, mnt_pocket_reform_sysctl_of_match);

static const struct spi_device_id mnt_pocket_reform_sysctl_id[] = {
	{ "lpc11u24", 0 },
	{ }
};
MODULE_DEVICE_TABLE(spi, mnt_pocket_reform_sysctl_id);

static struct spi_driver mnt_pocket_reform_sysctl_driver = {
	.probe = mnt_pocket_reform_sysctl_probe,
	.remove = mnt_pocket_reform_sysctl_remove,
	.driver = {
		.name = "mnt-pocket-reform-sysctl",
		.of_match_table = mnt_pocket_reform_sysctl_of_match,
	},
	.id_table = mnt_pocket_reform_sysctl_id,
};
module_spi_driver(mnt_pocket_reform_sysctl_driver);

MODULE_AUTHOR("Stephano Cetola");
MODULE_DESCRIPTION("MNT Pocket Reform RP2040 sysctl transport");
MODULE_LICENSE("GPL");
