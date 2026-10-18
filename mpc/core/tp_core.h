/* The Torpedo for the plugin: a worker thread owns the device (tp_dev) and keeps a shared copy of
 * its state, so the plugin's calls never wait on USB.
 *
 * - Changes are queued in order, one entry per parameter: writing a parameter again replaces its
 *   queued value and moves it to the end, so a Q-Link sweep sends a few values, not hundreds,
 *   and "mode, then a parameter of that mode" stays in that order.
 * - The state the plugin reads is the device's with the queued values on top, so the echo of an
 *   older value can't move a knob back while a newer one is on its way.
 * - Changes the device makes itself (front panel, linked parameters, preset loads, a refused
 *   write reverting) are collected for tp_core_take_changes; the plugin's own writes are not.
 * - When the device goes away (unplugged: ENODEV), the worker reopens it and reads everything
 *   again, since a replugged unit sends nothing on its own.
 * - The one thing that writes the unit's flash is tp_core_store, which the plugin only calls once the user has
 *   confirmed it on screen. Presets are otherwise edited, never stored. */
#pragma once
#include <stdint.h>

#include "tp_tables.h"

#define TP_MAX_BANK 256
#define TP_NAME_LEN 64

typedef struct {
    const char *path;   /* NULL: look for the hidraw node in sysfs at each attempt */
    int poll_ms;        /* idle polling, for front-panel changes and the meters (0: 100) */
    int retry_ms;       /* between connection attempts (0: 1000) */
} tp_core_config_t;

enum { TP_STORE_NONE, TP_STORE_OK, TP_STORE_FAILED };

typedef struct {
    int connected;
    unsigned generation;   /* +1 at each (re)connection */
    char serial[9], firmware[8];
    int params[TP_NPARAMS];   /* with the queued values on top */
    int setup[TP_NSETUP];
    uint8_t meter_in, meter_out, clip;
    int storing;               /* the preset being stored (queued or running, ~2 s), else -1 */
    int store_status;          /* how the last store ended: TP_STORE_* */
    int store_slot;            /* ...and which preset it was */
    unsigned store_seq;        /* +1 whenever one ends */
} tp_state_t;

enum { TP_CHANGED_CONNECTION = 1, TP_CHANGED_NAMES = 2, TP_CHANGED_CABS = 4, TP_CHANGED_IRS = 8 };

typedef struct {
    uint64_t params;   /* bit i: tp_params[i] */
    uint32_t setup;    /* bit i: tp_setup_names[i] */
    unsigned flags;    /* TP_CHANGED_* */
} tp_changes_t;

typedef struct {   /* a cab of bank 0, or an IR (no maker or mics) */
    char name[TP_NAME_LEN];
    char maker[33];
    char mics[8][TP_MIC_SLOT + 1];   /* SIMUHP_MIC / SIMUHP_MIC_2 index this; "" = empty slot */
} tp_cab_t;

typedef struct tp_core tp_core_t;

tp_core_t *tp_core_start(const tp_core_config_t *cfg);   /* NULL with errno if the thread can't start */
void tp_core_stop(tp_core_t *c);
void tp_core_state(tp_core_t *c, tp_state_t *s);
void tp_core_take_changes(tp_core_t *c, tp_changes_t *ch);   /* and clear them */
/* +1 whenever changes are noted, for several readers (plugin instances) that each keep the last one seen */
unsigned tp_core_revision(tp_core_t *c);

/* Queue a change: 0, or -1 with errno ENOTCONN, EINVAL (out of range or not writable), or EPERM
 * (not editable in arcade mode: the unit would drop it silently). */
int tp_core_set_param(tp_core_t *c, int index, int value);
int tp_core_set_setup(tp_core_t *c, int index, int value);   /* the settings in tp_setup_max */
int tp_core_select_preset(tp_core_t *c, int n);   /* 0..127; discards unsaved edits */
/* Select a cab of bank 0, moving mic A (and B, outside arcade mode) to the same mic name when the
 * new cab has it, like Torpedo Remote. */
int tp_core_select_cab(tp_core_t *c, int cab);
int tp_core_select_ir(tp_core_t *c, int slot, int ir);   /* IR slot 0/1, from the ML bank (folder 0) */
/* Store the current sound (the edit buffer) into preset n, 0..127, under `name`: NULL or "" keeps the buffer's own,
 * else 1..TP_NAME_MAX characters of TP_NAME_CHARS, given to the buffer first (`03 80`, device.py rename_buffer). A
 * flash write that takes about 2 s, after the changes queued before it. Afterwards the names are read back and
 * tp_state_t says how it went (store_status). Only one at a time: -1 with errno EBUSY. ENOTCONN, EINVAL (a preset
 * number or a name out of range) as above. */
int tp_core_store(tp_core_t *c, int n, const char *name);
/* Wait until every queued change has been handled: 0, or -1 with errno ETIMEDOUT. */
int tp_core_wait_idle(tp_core_t *c, int timeout_ms);

/* Lists read at connection: -1 (ERANGE) for an index past the end. */
int tp_core_cab_count(tp_core_t *c);
int tp_core_cab(tp_core_t *c, int i, tp_cab_t *out);
int tp_core_ir_count(tp_core_t *c);
int tp_core_ir_name(tp_core_t *c, int i, char *buf, int len);
/* n: 0..127, or TP_EDIT_BUFFER; *mode (if not NULL): the PRESET_MODE stored with it (TP_ARCADE...) */
int tp_core_preset_name(tp_core_t *c, int n, char *buf, int len, int *mode);
