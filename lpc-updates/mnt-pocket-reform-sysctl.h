#ifndef _LINUX_MNT_POCKET_REFORM_SYSCTL_H
#define _LINUX_MNT_POCKET_REFORM_SYSCTL_H

#include <linux/bits.h>
#include <linux/types.h>

#define MNT_POCKET_REFORM_SYSCTL_DISPLAY_CAP_BACKLIGHT	BIT(0)

int mnt_pocket_reform_sysctl_get_display_abi(u8 *version, u8 *caps);
int mnt_pocket_reform_sysctl_set_disp_en(bool enabled);
int mnt_pocket_reform_sysctl_get_disp_en(bool *enabled);
int mnt_pocket_reform_sysctl_set_disp_reset(bool asserted);
int mnt_pocket_reform_sysctl_get_disp_reset(bool *asserted);
int mnt_pocket_reform_sysctl_set_backlight_percent(u8 percent);

#endif
