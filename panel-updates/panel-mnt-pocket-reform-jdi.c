// SPDX-License-Identifier: GPL-2.0-only
/*
 * MNT Pocket Reform JDI LT070ME05000 panel driver
 *
 * This driver uses the Pocket Reform sysctl firmware display-control ABI
 * for panel control signals and keeps the panel sequencing in Linux.
 */

#include <linux/device/bus.h>
#include <linux/delay.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_device.h>
#include <linux/backlight.h>
#include <linux/gpio/consumer.h>
#include <linux/mnt-pocket-reform-sysctl.h>
#include <linux/regulator/consumer.h>

#include <video/mipi_display.h>

#include <drm/drm_connector.h>
#include <drm/drm_crtc.h>
#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>

#define MNT_SYSCTL_DISPLAY_ABI_VERSION		1

struct mnt_reform_jdi_panel {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct backlight_device *backlight;
	struct gpio_desc *enable_gpio;
	struct regulator_bulk_data supplies[2];
	u8 display_abi_version;
	u8 display_abi_caps;
	const struct drm_display_mode *mode;
	unsigned int display_v2_init_count;
	bool prepared;
	bool enabled;
};

struct mnt_reform_jdi_cmd {
	u8 len;
	u8 data[3];
};

static const char * const mnt_reform_jdi_supply_names[] = {
	"vddp",
	"iovcc",
};

static const struct drm_display_mode mnt_reform_jdi_default_mode = {
	.clock = 155493,
	.hdisplay = 1200,
	.hsync_start = 1200 + 48,
	.hsync_end = 1200 + 48 + 32,
	.htotal = 1200 + 48 + 32 + 60,
	.vdisplay = 1920,
	.vsync_start = 1920 + 3,
	.vsync_end = 1920 + 3 + 5,
	.vtotal = 1920 + 3 + 5 + 6,
};

static const struct drm_display_mode mnt_reform_jdi_v2_mode = {
	.clock = 140000,
	.hdisplay = 1200,
	.hsync_start = 1200 + 40,
	.hsync_end = 1200 + 40 + 20,
	.htotal = 1200 + 40 + 20 + 40,
	.vdisplay = 1920,
	.vsync_start = 1920 + 18,
	.vsync_end = 1920 + 18 + 2,
	.vtotal = 1920 + 18 + 2 + 20,
	.flags = DRM_MODE_FLAG_NHSYNC | DRM_MODE_FLAG_NVSYNC,
};

static const struct mnt_reform_jdi_cmd mnt_reform_jdi_v2_init_cmds[] = {
	{ 3, { 0xE0, 0xAB, 0xBA } },
	{ 2, { 0xFF, 0x01 } }, { 2, { 0x83, 0x16 } }, { 2, { 0x90, 0x81 } },
	{ 2, { 0xFF, 0x02 } }, { 2, { 0x80, 0xA0 } }, { 2, { 0x81, 0x50 } },
	{ 2, { 0x82, 0x50 } }, { 2, { 0xA2, 0xB0 } }, { 2, { 0xA3, 0x49 } },
	{ 2, { 0xA4, 0x6B } }, { 2, { 0xA5, 0x34 } }, { 2, { 0xA6, 0x22 } },
	{ 2, { 0xA7, 0x34 } }, { 2, { 0xA8, 0x22 } }, { 2, { 0xA9, 0x51 } },
	{ 2, { 0xAA, 0xD4 } }, { 2, { 0xAB, 0xA6 } }, { 2, { 0xAC, 0x05 } },
	{ 2, { 0xAD, 0xAB } }, { 2, { 0xAE, 0x3B } }, { 2, { 0xAF, 0x1B } },
	{ 2, { 0xB0, 0x1E } }, { 2, { 0xB1, 0xAB } }, { 2, { 0xB2, 0xC1 } },
	{ 2, { 0xB3, 0x22 } }, { 2, { 0xB4, 0xFF } }, { 2, { 0xB5, 0xCC } },
	{ 2, { 0xB6, 0x23 } }, { 2, { 0xB7, 0x01 } }, { 2, { 0xBF, 0x00 } },
	{ 2, { 0xC0, 0x0E } }, { 2, { 0xC1, 0x00 } }, { 2, { 0xC2, 0xFF } },
	{ 2, { 0xC3, 0x08 } }, { 2, { 0xC5, 0xFF } }, { 2, { 0xC7, 0x00 } },
	{ 2, { 0xC8, 0x05 } }, { 2, { 0xC9, 0x12 } }, { 2, { 0xCA, 0x29 } },
	{ 2, { 0xCB, 0x41 } }, { 2, { 0xCE, 0x06 } }, { 2, { 0xD1, 0x7F } },
	{ 2, { 0xD2, 0x6A } }, { 2, { 0xD3, 0x63 } }, { 2, { 0xD4, 0x63 } },
	{ 2, { 0xD5, 0x61 } }, { 2, { 0xD6, 0x61 } }, { 2, { 0xD7, 0x5D } },
	{ 2, { 0xD8, 0x5B } }, { 2, { 0xD9, 0x55 } }, { 2, { 0xDA, 0x4D } },
	{ 2, { 0xDB, 0x4B } }, { 2, { 0xDC, 0x49 } }, { 2, { 0xDD, 0x47 } },
	{ 2, { 0xDE, 0x43 } }, { 2, { 0xDF, 0x27 } }, { 2, { 0xE0, 0x14 } },
	{ 2, { 0xE1, 0x00 } }, { 2, { 0xE2, 0x7F } }, { 2, { 0xE3, 0x6A } },
	{ 2, { 0xE4, 0x63 } }, { 2, { 0xE5, 0x63 } }, { 2, { 0xE6, 0x61 } },
	{ 2, { 0xE7, 0x61 } }, { 2, { 0xE8, 0x5D } }, { 2, { 0xE9, 0x5B } },
	{ 2, { 0xEA, 0x55 } }, { 2, { 0xEB, 0x4D } }, { 2, { 0xEC, 0x4B } },
	{ 2, { 0xED, 0x49 } }, { 2, { 0xEE, 0x47 } }, { 2, { 0xEF, 0x43 } },
	{ 2, { 0xF0, 0x27 } }, { 2, { 0xF1, 0x14 } }, { 2, { 0xF2, 0x00 } },
	{ 2, { 0xFF, 0x03 } }, { 2, { 0x9D, 0x00 } }, { 2, { 0xFF, 0x04 } },
	{ 2, { 0x80, 0xCD } }, { 2, { 0x81, 0xCB } }, { 2, { 0x82, 0xA9 } },
	{ 2, { 0x83, 0x8C } }, { 2, { 0x84, 0x46 } }, { 2, { 0x85, 0x66 } },
	{ 2, { 0x86, 0x66 } }, { 2, { 0x87, 0x64 } }, { 2, { 0x88, 0xA0 } },
	{ 2, { 0x89, 0x08 } }, { 2, { 0x8A, 0xA0 } }, { 2, { 0x8B, 0x08 } },
	{ 2, { 0x8C, 0x02 } }, { 2, { 0x8D, 0x81 } }, { 2, { 0x8E, 0x00 } },
	{ 2, { 0x8F, 0x00 } }, { 2, { 0x90, 0x00 } }, { 2, { 0x91, 0x05 } },
	{ 2, { 0x92, 0x00 } }, { 2, { 0x93, 0x0F } }, { 2, { 0x94, 0x00 } },
	{ 2, { 0x95, 0x08 } }, { 2, { 0x96, 0x00 } }, { 2, { 0x97, 0x00 } },
	{ 2, { 0x98, 0x00 } }, { 2, { 0x99, 0x00 } }, { 2, { 0x9A, 0x00 } },
	{ 2, { 0x9B, 0x01 } }, { 2, { 0x9C, 0x00 } }, { 2, { 0x9D, 0x74 } },
	{ 2, { 0x9E, 0x12 } }, { 2, { 0x9F, 0xB2 } }, { 2, { 0xA0, 0x07 } },
	{ 2, { 0xA1, 0xA0 } }, { 2, { 0xA2, 0x08 } }, { 2, { 0xA3, 0x60 } },
	{ 2, { 0xA4, 0x08 } }, { 2, { 0xA5, 0x02 } }, { 2, { 0xA6, 0x54 } },
	{ 2, { 0xA7, 0x80 } }, { 2, { 0xA8, 0x00 } }, { 2, { 0xA9, 0x00 } },
	{ 2, { 0xAA, 0x00 } }, { 2, { 0xAB, 0x00 } }, { 2, { 0xAC, 0x00 } },
	{ 2, { 0xAD, 0x00 } }, { 2, { 0xAE, 0x00 } }, { 2, { 0xAF, 0x00 } },
	{ 2, { 0xB0, 0x00 } }, { 2, { 0xB1, 0x00 } }, { 2, { 0xB2, 0x00 } },
	{ 2, { 0xB3, 0x00 } }, { 2, { 0xB4, 0x09 } }, { 2, { 0xB5, 0x01 } },
	{ 2, { 0xB6, 0x13 } }, { 2, { 0xB7, 0x08 } }, { 2, { 0xB8, 0x12 } },
	{ 2, { 0xB9, 0x00 } }, { 2, { 0xBA, 0x03 } }, { 2, { 0xBB, 0x00 } },
	{ 2, { 0xBC, 0x00 } }, { 2, { 0xBD, 0x01 } }, { 2, { 0xBE, 0x70 } },
	{ 2, { 0xBF, 0xBA } }, { 2, { 0xC0, 0x98 } }, { 2, { 0xC1, 0x76 } },
	{ 2, { 0xC2, 0x54 } }, { 2, { 0xC3, 0x32 } }, { 2, { 0xC4, 0x10 } },
	{ 2, { 0xC5, 0xCD } }, { 2, { 0xC6, 0xEF } }, { 2, { 0xC7, 0x00 } },
	{ 2, { 0xC8, 0x00 } }, { 2, { 0xC9, 0x20 } }, { 2, { 0xCB, 0x21 } },
	{ 2, { 0xCD, 0x0B } }, { 2, { 0xCF, 0x10 } }, { 2, { 0xD0, 0x12 } },
	{ 2, { 0xD1, 0x14 } }, { 2, { 0xD2, 0x16 } }, { 2, { 0xD3, 0x18 } },
	{ 2, { 0xD4, 0x1A } }, { 2, { 0xD5, 0x05 } }, { 2, { 0xD6, 0x07 } },
	{ 2, { 0xD7, 0x09 } }, { 2, { 0xD9, 0x00 } }, { 2, { 0xDA, 0x00 } },
	{ 2, { 0xDB, 0x00 } }, { 2, { 0xDC, 0x00 } }, { 2, { 0xDD, 0x20 } },
	{ 2, { 0xDF, 0x21 } }, { 2, { 0xE3, 0x0B } }, { 2, { 0xE5, 0x11 } },
	{ 2, { 0xE6, 0x13 } }, { 2, { 0xE7, 0x15 } }, { 2, { 0xE8, 0x17 } },
	{ 2, { 0xE9, 0x19 } }, { 2, { 0xEA, 0x1B } }, { 2, { 0xEB, 0x06 } },
	{ 2, { 0xEC, 0x08 } }, { 2, { 0xED, 0x0A } }, { 2, { 0xEF, 0x00 } },
	{ 2, { 0xF0, 0x00 } }, { 2, { 0xF1, 0xA2 } }, { 2, { 0xF2, 0x18 } },
	{ 2, { 0xF3, 0x11 } }, { 2, { 0xF4, 0x00 } }, { 2, { 0xF5, 0x11 } },
	{ 2, { 0xF6, 0x00 } }, { 2, { 0xF7, 0x11 } }, { 2, { 0xF8, 0x00 } },
	{ 2, { 0xF9, 0x00 } }, { 2, { 0xFA, 0x00 } }, { 2, { 0xFB, 0x00 } },
	{ 2, { 0xFC, 0x20 } }, { 2, { 0xFD, 0x00 } }, { 2, { 0xFE, 0x00 } },
	{ 2, { 0xFF, 0x05 } }, { 2, { 0x8F, 0xE8 } }, { 2, { 0x90, 0x84 } },
	{ 2, { 0x91, 0x77 } }, { 2, { 0xFF, 0x06 } }, { 2, { 0x80, 0x10 } },
	{ 2, { 0x81, 0x00 } }, { 2, { 0x82, 0x07 } }, { 2, { 0x83, 0x9F } },
	{ 2, { 0xFF, 0x07 } }, { 2, { 0x80, 0x14 } }, { 2, { 0x81, 0x12 } },
	{ 2, { 0x82, 0x02 } }, { 2, { 0x83, 0x20 } }, { 2, { 0x84, 0x24 } },
	{ 2, { 0x85, 0x0C } }, { 2, { 0x86, 0x39 } }, { 2, { 0x87, 0x77 } },
	{ 2, { 0x88, 0x80 } }, { 2, { 0x89, 0x33 } }, { 2, { 0x8A, 0x00 } },
	{ 2, { 0x8B, 0x04 } }, { 2, { 0x8C, 0x09 } }, { 2, { 0x8D, 0x40 } },
	{ 2, { 0x8E, 0x40 } }, { 2, { 0x91, 0x11 } }, { 2, { 0x92, 0x2C } },
	{ 2, { 0x93, 0x2D } }, { 2, { 0x94, 0x03 } }, { 2, { 0xA0, 0x0A } },
	{ 2, { 0xA2, 0x13 } }, { 2, { 0xFF, 0x00 } },
};

static inline struct mnt_reform_jdi_panel *to_mnt_reform_jdi_panel(struct drm_panel *panel)
{
	return container_of(panel, struct mnt_reform_jdi_panel, panel);
}

static int mnt_reform_jdi_display_on(struct mnt_reform_jdi_panel *jdi);

static int mnt_reform_jdi_bl_get_brightness(struct backlight_device *bl)
{
	return bl->props.brightness;
}

static int mnt_reform_jdi_bl_update_status(struct backlight_device *bl)
{
	u8 brightness = backlight_get_brightness(bl);
	int ret;

	ret = mnt_pocket_reform_sysctl_set_backlight_percent(brightness);
	return ret;
}

static const struct backlight_ops mnt_reform_jdi_bl_ops = {
	.update_status = mnt_reform_jdi_bl_update_status,
	.get_brightness = mnt_reform_jdi_bl_get_brightness,
};

static struct backlight_device *
mnt_reform_jdi_create_sysctl_backlight(struct mnt_reform_jdi_panel *jdi)
{
	struct device *dev = &jdi->dsi->dev;
	struct backlight_properties props = {
		.type = BACKLIGHT_RAW,
		.brightness = 100,
		.max_brightness = 100,
	};

	return devm_backlight_device_register(dev, dev_name(dev), dev, jdi,
					      &mnt_reform_jdi_bl_ops, &props);
}

static int mnt_reform_jdi_add(struct mnt_reform_jdi_panel *jdi)
{
	struct device *dev = &jdi->dsi->dev;
	int ret;
	unsigned int i;

	jdi->mode = &mnt_reform_jdi_v2_mode;

	for (i = 0; i < ARRAY_SIZE(jdi->supplies); i++)
		jdi->supplies[i].supply = mnt_reform_jdi_supply_names[i];

	ret = devm_regulator_bulk_get(dev, ARRAY_SIZE(jdi->supplies),
				      jdi->supplies);
	if (ret < 0)
		return dev_err_probe(dev, ret,
				     "failed to get panel supplies\n");

	ret = drm_panel_of_backlight(&jdi->panel);
	if (ret < 0)
		return dev_err_probe(dev, ret,
				     "failed to get panel backlight\n");

	jdi->enable_gpio = devm_gpiod_get_optional(dev, "enable", GPIOD_OUT_LOW);
	if (IS_ERR(jdi->enable_gpio))
		return dev_err_probe(dev, PTR_ERR(jdi->enable_gpio),
				     "failed to get panel enable gpio\n");

	if (!jdi->panel.backlight) {
		if (!(jdi->display_abi_caps &
		      MNT_POCKET_REFORM_SYSCTL_DISPLAY_CAP_BACKLIGHT))
			return dev_err_probe(dev, -ENODEV,
					     "firmware lacks required v2 backlight capability\n");

		jdi->backlight = mnt_reform_jdi_create_sysctl_backlight(jdi);
		if (IS_ERR(jdi->backlight))
			return dev_err_probe(dev, PTR_ERR(jdi->backlight),
					     "failed to register sysctl backlight\n");
	}

	drm_panel_add(&jdi->panel);

	return 0;
}

static void mnt_reform_jdi_del(struct mnt_reform_jdi_panel *jdi)
{
	if (jdi->panel.dev)
		drm_panel_remove(&jdi->panel);
}

static int mnt_reform_sysctl_get_display_abi(struct mnt_reform_jdi_panel *jdi)
{
	struct device *dev = &jdi->dsi->dev;
	int ret;

	ret = mnt_pocket_reform_sysctl_get_display_abi(&jdi->display_abi_version,
						       &jdi->display_abi_caps);
	if (ret < 0)
		return dev_err_probe(dev, ret,
				     "failed to query sysctl display ABI\n");

	if (jdi->display_abi_version != MNT_SYSCTL_DISPLAY_ABI_VERSION)
		return dev_err_probe(&jdi->dsi->dev, -ENODEV,
				     "sysctl display ABI mismatch: got %u need %u\n",
				     jdi->display_abi_version,
				     MNT_SYSCTL_DISPLAY_ABI_VERSION);

	dev_info(dev, "sysctl display ABI ok: version=%u caps=0x%02x\n",
		 jdi->display_abi_version,
		 jdi->display_abi_caps);

	return 0;
}

static int mnt_reform_sysctl_set_bool(struct mnt_reform_jdi_panel *jdi, u8 cmd,
				      bool value)
{
	struct device *dev = &jdi->dsi->dev;
	int ret;

	switch (cmd) {
	case 'E':
		ret = mnt_pocket_reform_sysctl_set_disp_en(value);
		break;
	case 'R':
		ret = mnt_pocket_reform_sysctl_set_disp_reset(value);
		break;
	default:
		ret = -EINVAL;
	}

	if (ret < 0)
		return dev_err_probe(dev, ret,
				     "sysctl display command 0x%02x failed\n",
				     cmd);

	return 0;
}

static int mnt_reform_sysctl_get_bool(struct mnt_reform_jdi_panel *jdi, u8 cmd,
				      bool *value)
{
	struct device *dev = &jdi->dsi->dev;
	int ret;

	switch (cmd) {
	case 'e':
		ret = mnt_pocket_reform_sysctl_get_disp_en(value);
		break;
	case 'r':
		ret = mnt_pocket_reform_sysctl_get_disp_reset(value);
		break;
	default:
		ret = -EINVAL;
	}

	if (ret < 0)
		return dev_err_probe(dev, ret,
				     "sysctl display command 0x%02x failed\n",
				     cmd);

	return 0;
}

static int mnt_reform_sysctl_set_disp_en(struct mnt_reform_jdi_panel *jdi, bool enabled)
{
	return mnt_reform_sysctl_set_bool(jdi, 'E', enabled);
}

static int mnt_reform_sysctl_get_disp_en(struct mnt_reform_jdi_panel *jdi, bool *enabled)
{
	return mnt_reform_sysctl_get_bool(jdi, 'e', enabled);
}

static int mnt_reform_sysctl_set_disp_reset(struct mnt_reform_jdi_panel *jdi, bool asserted)
{
	return mnt_reform_sysctl_set_bool(jdi, 'R', asserted);
}

static int mnt_reform_sysctl_get_disp_reset(struct mnt_reform_jdi_panel *jdi, bool *asserted)
{
	return mnt_reform_sysctl_get_bool(jdi, 'r', asserted);
}

static void mnt_reform_sysctl_cleanup(struct mnt_reform_jdi_panel *jdi) {}

static int mnt_reform_sysctl_init(struct mnt_reform_jdi_panel *jdi)
{
	int ret;

	ret = mnt_reform_sysctl_get_display_abi(jdi);
	if (ret < 0)
		mnt_reform_sysctl_cleanup(jdi);

	return ret;
}

static int mnt_reform_jdi_write_raw_lpm(struct mipi_dsi_device *dsi,
					const u8 *data, size_t len)
{
	const struct mipi_dsi_msg msg = {
		.channel = dsi->channel,
		.tx_buf = data,
		.tx_len = len,
		.type = MIPI_DSI_DCS_LONG_WRITE,
		.flags = MIPI_DSI_MSG_USE_LPM,
	};
	ssize_t ret;

	ret = dsi->host->ops->transfer(dsi->host, &msg);
	if (ret < 0)
		return ret;

	return 0;
}

static int mnt_reform_jdi_panel_init(struct mnt_reform_jdi_panel *jdi)
{
	struct mipi_dsi_device *dsi = jdi->dsi;
	struct device *dev = &dsi->dev;
	unsigned long mode_flags;
	unsigned int i;
	u32 init_cmd_limit = 0;
	u32 post_table_delay_ms = 0;
	u32 post_exit_sleep_delay_ms = 0;
	u32 post_display_on_delay_ms = 0;
	unsigned int cmd_count;
	bool send_v2_tables;
	int ret;

	dev_info(dev, "panel init: v2 path (count=%u)\n",
		 jdi->display_v2_init_count);

	if (of_property_present(dev->of_node, "probe-mode-lpm"))
		dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	else
		dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;

	/* Match the old v2 path's host-facing DSI flags first. */
	dsi->mode_flags &= ~MIPI_DSI_MODE_VIDEO_HSE;
	dsi->mode_flags &= ~(MIPI_DSI_MODE_NO_EOT_PACKET |
			     MIPI_DSI_MODE_VIDEO_NO_HFP |
			     MIPI_DSI_MODE_VIDEO_NO_HBP |
			     MIPI_DSI_MODE_VIDEO_NO_HSA);

	mode_flags = dsi->mode_flags;
	if (of_property_present(dev->of_node, "probe-mode-lpm"))
		dsi->mode_flags |= MIPI_DSI_MODE_LPM;
	else
		dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;

	ret = mipi_dsi_dcs_soft_reset(dsi);
	if (ret < 0)
		goto err;

	mdelay(20);

	send_v2_tables = true;
	if (send_v2_tables) {
		cmd_count = ARRAY_SIZE(mnt_reform_jdi_v2_init_cmds);
		if (!of_property_read_u32(dev->of_node, "mnt,v2-init-cmd-limit",
					  &init_cmd_limit) &&
		    init_cmd_limit > 0 && init_cmd_limit < cmd_count) {
			cmd_count = init_cmd_limit;
			dev_info(dev, "panel init: limiting legacy v2 tables to first %u commands\n",
				 cmd_count);
		}

		for (i = 0; i < cmd_count; i++) {
			ret = mnt_reform_jdi_write_raw_lpm(dsi,
							   mnt_reform_jdi_v2_init_cmds[i].data,
							   mnt_reform_jdi_v2_init_cmds[i].len);
			if (ret < 0)
				goto err;
		}

		dev_info(dev, "panel init: legacy v2 tables sent (%u/%zu commands)\n",
			 cmd_count, ARRAY_SIZE(mnt_reform_jdi_v2_init_cmds));

		if (!of_property_read_u32(dev->of_node, "mnt,post-table-delay-ms",
					  &post_table_delay_ms) &&
		    post_table_delay_ms > 0) {
			dev_info(dev, "panel init: post-table delay %u ms\n",
				 post_table_delay_ms);
			msleep(post_table_delay_ms);
		} else {
			msleep(20);
		}
	}

	jdi->display_v2_init_count++;

	ret = mipi_dsi_dcs_exit_sleep_mode(dsi);
	if (ret < 0)
		goto err;

	if (!of_property_read_u32(dev->of_node, "mnt,post-exit-sleep-delay-ms",
				  &post_exit_sleep_delay_ms) &&
	    post_exit_sleep_delay_ms > 0) {
		dev_info(dev, "panel init: post-exit-sleep delay %u ms\n",
			 post_exit_sleep_delay_ms);
		msleep(post_exit_sleep_delay_ms);
	} else {
		msleep(120);
	}

	ret = mnt_reform_jdi_display_on(jdi);
	if (ret < 0)
		goto err;

	if (!of_property_read_u32(dev->of_node, "mnt,post-display-on-delay-ms",
				  &post_display_on_delay_ms) &&
	    post_display_on_delay_ms > 0) {
		dev_info(dev, "panel init: post-display-on delay %u ms\n",
			 post_display_on_delay_ms);
		msleep(post_display_on_delay_ms);
	}

	dev_info(dev, "panel init: v2 path ready\n");
	dsi->mode_flags = mode_flags;

	return 0;

err:
	dsi->mode_flags = mode_flags;
	return dev_err_probe(dev, ret, "failed to initialize temporary v2 panel path\n");
}

static int mnt_reform_jdi_display_on(struct mnt_reform_jdi_panel *jdi)
{
	struct mipi_dsi_device *dsi = jdi->dsi;
	struct device *dev = &dsi->dev;
	int ret;

	ret = mipi_dsi_dcs_set_display_on(dsi);
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to turn display on\n");

	return 0;
}

static int mnt_reform_jdi_display_off(struct mnt_reform_jdi_panel *jdi)
{
	struct mipi_dsi_device *dsi = jdi->dsi;
	struct device *dev = &dsi->dev;
	unsigned long mode_flags;
	int ret;

	mode_flags = dsi->mode_flags;
	dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;

	ret = mipi_dsi_dcs_set_display_off(dsi);
	dsi->mode_flags = mode_flags;
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to turn display off\n");

	return 0;
}

static int mnt_reform_jdi_sleep_in(struct mnt_reform_jdi_panel *jdi)
{
	struct mipi_dsi_device *dsi = jdi->dsi;
	struct device *dev = &dsi->dev;
	unsigned long mode_flags;
	int ret;

	mode_flags = dsi->mode_flags;
	dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;

	ret = mipi_dsi_dcs_enter_sleep_mode(dsi);
	dsi->mode_flags = mode_flags;
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to enter sleep mode\n");

	return 0;
}

static int mnt_reform_jdi_disable(struct drm_panel *panel)
{
	struct mnt_reform_jdi_panel *jdi = to_mnt_reform_jdi_panel(panel);
	struct device *dev = &jdi->dsi->dev;
	int ret;

	if (!jdi->enabled)
		return 0;

	if (jdi->panel.backlight)
		backlight_disable(jdi->panel.backlight);
	else if (jdi->backlight)
		backlight_disable(jdi->backlight);

	dev_info(dev, "disable: display off\n");

	ret = mnt_reform_jdi_display_off(jdi);
	if (ret < 0)
		return ret;

	jdi->enabled = false;
	dev_info(dev, "disable: done\n");

	return 0;
}

static int mnt_reform_jdi_powerdown(struct mnt_reform_jdi_panel *jdi)
{
	struct device *dev = &jdi->dsi->dev;
	int ret = 0;
	int disable_ret;

	if (!jdi->prepared)
		return 0;

	if (jdi->enabled) {
		disable_ret = mnt_reform_jdi_disable(&jdi->panel);
		if (disable_ret < 0) {
			dev_warn(dev, "display disable during unprepare failed: %d\n",
				 disable_ret);
			ret = disable_ret;
		}
	}

	disable_ret = mnt_reform_sysctl_set_disp_reset(jdi, true);
	if (disable_ret < 0) {
		dev_err(dev, "failed to assert panel reset: %d\n", disable_ret);
		if (!ret)
			ret = disable_ret;
	}

	if (jdi->enable_gpio)
		gpiod_set_value_cansleep(jdi->enable_gpio, 0);

	disable_ret = mnt_reform_sysctl_set_disp_en(jdi, false);
	if (disable_ret < 0) {
		dev_err(dev, "failed to disable panel power: %d\n", disable_ret);
		if (!ret)
			ret = disable_ret;
	}

	disable_ret = regulator_bulk_disable(ARRAY_SIZE(jdi->supplies),
					     jdi->supplies);
	if (disable_ret < 0) {
		dev_err(dev, "failed to disable panel supplies: %d\n", disable_ret);
		if (!ret)
			ret = disable_ret;
	}

	jdi->prepared = false;
	dev_info(dev, "powerdown: panel rails off\n");

	return ret;
}

static int mnt_reform_jdi_unprepare(struct drm_panel *panel)
{
	struct mnt_reform_jdi_panel *jdi = to_mnt_reform_jdi_panel(panel);

	return mnt_reform_jdi_powerdown(jdi);
}

static int mnt_reform_jdi_prepare(struct drm_panel *panel)
{
	struct mnt_reform_jdi_panel *jdi = to_mnt_reform_jdi_panel(panel);
	struct device *dev = &jdi->dsi->dev;
	u32 post_rails_delay_ms = 0;
	u32 post_disp_en_delay_ms = 0;
	u32 post_reset_delay_ms = 0;
	u32 pre_init_settle_delay_ms = 0;
	u32 post_gpio_settle_delay_ms = 0;
	int ret;

	if (jdi->prepared)
		return 0;

	dev_info(dev, "prepare: enable panel supplies\n");

	ret = regulator_bulk_enable(ARRAY_SIZE(jdi->supplies), jdi->supplies);
	if (ret < 0)
		return dev_err_probe(dev, ret, "failed to enable panel supplies\n");

	/*
	 * Match the old working driver ordering/timing first:
	 * 20ms after rails, then DCDC_EN, short settle, reset release,
	 * short settle, enable GPIO, short settle, then panel init.
	 */
	if (!of_property_read_u32(dev->of_node, "mnt,post-rails-delay-ms",
				  &post_rails_delay_ms) &&
	    post_rails_delay_ms > 0) {
		dev_info(dev, "prepare: post-rails delay %u ms\n",
			 post_rails_delay_ms);
		msleep(post_rails_delay_ms);
	} else {
		msleep(20);
	}

	dev_info(dev, "prepare: assert disp_en\n");

	ret = mnt_reform_sysctl_set_disp_en(jdi, true);
	if (ret < 0)
		goto err_poweroff;

	if (!of_property_read_u32(dev->of_node, "mnt,post-disp-en-delay-ms",
				  &post_disp_en_delay_ms) &&
	    post_disp_en_delay_ms > 0) {
		dev_info(dev, "prepare: post-disp-en delay %u ms\n",
			 post_disp_en_delay_ms);
		msleep(post_disp_en_delay_ms);
	} else {
		usleep_range(10, 20);
	}

	dev_info(dev, "prepare: release panel reset\n");

	ret = mnt_reform_sysctl_set_disp_reset(jdi, false);
	if (ret < 0)
		goto err_poweroff;

	if (!of_property_read_u32(dev->of_node, "mnt,post-reset-delay-ms",
				  &post_reset_delay_ms) &&
	    post_reset_delay_ms > 0) {
		dev_info(dev, "prepare: post-reset delay %u ms\n",
			 post_reset_delay_ms);
		msleep(post_reset_delay_ms);
	} else {
		usleep_range(10, 20);
	}

	if (jdi->enable_gpio) {
		dev_info(dev, "prepare: assert enable gpio\n");
		gpiod_set_value_cansleep(jdi->enable_gpio, 1);
		usleep_range(10, 20);
	}

	if (!of_property_read_u32(dev->of_node, "mnt,post-gpio-settle-delay-ms",
				  &post_gpio_settle_delay_ms) &&
	    post_gpio_settle_delay_ms > 0) {
		dev_info(dev, "prepare: post-gpio settle delay %u ms\n",
			 post_gpio_settle_delay_ms);
		msleep(post_gpio_settle_delay_ms);
	}

	if (!of_property_read_u32(dev->of_node, "mnt,pre-init-settle-delay-ms",
				  &pre_init_settle_delay_ms) &&
	    pre_init_settle_delay_ms > 0) {
		dev_info(dev, "prepare: pre-init settle delay %u ms\n",
			 pre_init_settle_delay_ms);
		msleep(pre_init_settle_delay_ms);
	}

	dev_info(dev, "prepare: start panel init\n");

	ret = mnt_reform_jdi_panel_init(jdi);
	if (ret < 0)
		goto err_poweroff;

	jdi->prepared = true;
	dev_info(dev, "prepare: done\n");

	return 0;

err_poweroff:
	if (jdi->enable_gpio)
		gpiod_set_value_cansleep(jdi->enable_gpio, 0);
	mnt_reform_sysctl_set_disp_reset(jdi, true);
	mnt_reform_sysctl_set_disp_en(jdi, false);
	regulator_bulk_disable(ARRAY_SIZE(jdi->supplies), jdi->supplies);

	return dev_err_probe(dev, ret, "failed to power up panel\n");
}

static int mnt_reform_jdi_enable(struct drm_panel *panel)
{
	struct mnt_reform_jdi_panel *jdi = to_mnt_reform_jdi_panel(panel);
	struct device *dev = &jdi->dsi->dev;
	int ret;

	if (jdi->enabled)
		return 0;

	dev_info(dev, "enable: start\n");

	if (jdi->panel.backlight)
		backlight_enable(jdi->panel.backlight);
	else if (jdi->backlight)
		backlight_enable(jdi->backlight);

	jdi->enabled = true;
	dev_info(dev, "enable: display on complete\n");

	return 0;
}

static int mnt_reform_jdi_get_modes(struct drm_panel *panel,
				    struct drm_connector *connector)
{
	struct mnt_reform_jdi_panel *jdi = to_mnt_reform_jdi_panel(panel);
	struct drm_display_mode *mode;
	struct device *dev = &jdi->dsi->dev;
	u32 vsync_shift = 0;

	mode = drm_mode_duplicate(connector->dev, jdi->mode);
	if (!mode)
		return -ENOMEM;

	if (of_property_present(dev->of_node, "vsync-shift")) {
		of_property_read_u32(dev->of_node, "vsync-shift", &vsync_shift);
		dev_warn(dev, "vsync-shift from device tree: %u\n", vsync_shift);
		mode->vsync_start += vsync_shift;
		mode->vsync_end += vsync_shift;
	}

	drm_mode_set_name(mode);
	drm_mode_probed_add(connector, mode);

	connector->display_info.width_mm = 95;
	connector->display_info.height_mm = 151;

	return 1;
}

static enum drm_panel_orientation
mnt_reform_jdi_get_orientation(struct drm_panel *panel)
{
	return DRM_MODE_PANEL_ORIENTATION_LEFT_UP;
}

static const struct drm_panel_funcs mnt_reform_jdi_panel_funcs = {
	.disable = mnt_reform_jdi_disable,
	.unprepare = mnt_reform_jdi_unprepare,
	.prepare = mnt_reform_jdi_prepare,
	.enable = mnt_reform_jdi_enable,
	.get_modes = mnt_reform_jdi_get_modes,
	.get_orientation = mnt_reform_jdi_get_orientation,
};

static const struct of_device_id mnt_reform_jdi_of_match[] = {
	{ .compatible = "mnt,pocket-reform-jdi", },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, mnt_reform_jdi_of_match);

static int mnt_reform_jdi_probe(struct mipi_dsi_device *dsi)
{
	struct mnt_reform_jdi_panel *jdi;
	int ret;
	bool signal;

	dsi->lanes = 4;
	dsi->format = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_VIDEO | MIPI_DSI_MODE_VIDEO_HSE;

	if (of_property_present(dsi->dev.of_node, "burst-mode")) {
		dsi->mode_flags |= MIPI_DSI_MODE_VIDEO_BURST;
		dev_warn(&dsi->dev, "DSI burst mode enabled via device tree\n");
	}

	if (of_property_present(dsi->dev.of_node, "no-eot-hfp-hbp-hsa")) {
		dsi->mode_flags |= MIPI_DSI_MODE_NO_EOT_PACKET |
				   MIPI_DSI_MODE_VIDEO_NO_HFP |
				   MIPI_DSI_MODE_VIDEO_NO_HBP |
				   MIPI_DSI_MODE_VIDEO_NO_HSA;
		dev_warn(&dsi->dev,
			 "DSI eot/hfp/hbp/hsa disabled via device tree\n");
	}

	jdi = devm_drm_panel_alloc(&dsi->dev, struct mnt_reform_jdi_panel, panel,
				   &mnt_reform_jdi_panel_funcs,
				   DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(jdi))
		return PTR_ERR(jdi);

	jdi->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, jdi);

	dev_info(&dsi->dev, "Pocket Reform JDI panel probe start\n");

	ret = mnt_reform_sysctl_init(jdi);
	if (ret < 0)
		return ret;

	/* Fail probe early if the required sysctl display-control ABI is absent. */
	ret = mnt_reform_sysctl_get_disp_en(jdi, &signal);
	if (ret < 0)
		goto err_sysctl;

	ret = mnt_reform_sysctl_get_disp_reset(jdi, &signal);
	if (ret < 0)
		goto err_sysctl;

	ret = mnt_reform_jdi_add(jdi);
	if (ret < 0)
		goto err_sysctl;

	ret = mipi_dsi_attach(dsi);
	if (ret < 0)
		goto err_panel_add;

	dev_info(&dsi->dev, "Pocket Reform JDI panel probe complete\n");

	return 0;

err_panel_add:
	mnt_reform_jdi_del(jdi);
err_sysctl:
	mnt_reform_sysctl_cleanup(jdi);
	return ret;
}

static void mnt_reform_jdi_remove(struct mipi_dsi_device *dsi)
{
	struct mnt_reform_jdi_panel *jdi = mipi_dsi_get_drvdata(dsi);
	int ret;

	ret = mnt_reform_jdi_powerdown(jdi);
	if (ret < 0)
		dev_warn(&dsi->dev, "failed to power down panel during remove: %d\n",
			 ret);

	ret = mipi_dsi_detach(dsi);
	if (ret < 0)
		dev_err(&dsi->dev, "failed to detach from DSI host: %d\n", ret);

	mnt_reform_jdi_del(jdi);
	mnt_reform_sysctl_cleanup(jdi);
}

static void mnt_reform_jdi_shutdown(struct mipi_dsi_device *dsi)
{
	struct mnt_reform_jdi_panel *jdi = mipi_dsi_get_drvdata(dsi);
	int ret;

	if (!jdi)
		return;

	ret = mnt_reform_jdi_powerdown(jdi);
	if (ret < 0)
		dev_warn(&dsi->dev, "failed to power down panel during shutdown: %d\n",
			 ret);
}

static struct mipi_dsi_driver mnt_reform_jdi_driver = {
	.driver = {
		.name = "panel-mnt-pocket-reform-jdi",
		.of_match_table = mnt_reform_jdi_of_match,
	},
	.probe = mnt_reform_jdi_probe,
	.remove = mnt_reform_jdi_remove,
	.shutdown = mnt_reform_jdi_shutdown,
};
module_mipi_dsi_driver(mnt_reform_jdi_driver);

MODULE_AUTHOR("Stephano Cetola");
MODULE_DESCRIPTION("MNT Pocket Reform JDI LT070ME05000 panel");
MODULE_LICENSE("GPL");
