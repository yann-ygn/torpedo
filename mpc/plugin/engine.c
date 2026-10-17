/* The Torpedo Live as an MPC plugin engine (mpc-vst-plugins wrapper/engine.h): a silent instrument whose
 * parameters are the unit's, through one tp_core shared by every instance in the process (two cores would
 * fight over the device). Parameters are listed in params.json, generated with plugin_tables.c by
 * gen_plugin.py.
 *
 * - Values read are the core's (the device's with queued writes on top); writes are queued, never waited on.
 * - "display_rev" is the core's revision, so the wrapper (HAS_DISPLAY_REV) reports what the unit changed by
 *   itself (front panel, linked parameters, preset loads, the connection) and redraws texts and when= panels.
 * - No "state": nothing is saved with the project or pushed to the unit when one loads.
 * - TORPEDO_DEVICE in the environment overrides the device path (tests: unix:<socket>). */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "engine.h"
#include "plugin_tables.h"
#include "tp_core.h"

static pthread_mutex_t shared_lock = PTHREAD_MUTEX_INITIALIZER;
static tp_core_t *shared_core;
static int users;

typedef struct {
    tp_core_t *core;
} inst_t;

static void *create(const char *data_dir)
{
    (void)data_dir;
    inst_t *in = calloc(1, sizeof *in);
    if (!in)
        return NULL;
    pthread_mutex_lock(&shared_lock);
    if (!shared_core) {
        tp_core_config_t cfg = {.path = getenv("TORPEDO_DEVICE")};
        shared_core = tp_core_start(&cfg);
    }
    if (shared_core)
        users++;
    in->core = shared_core;
    pthread_mutex_unlock(&shared_lock);
    if (!in->core) {
        free(in);
        return NULL;
    }
    return in;
}

static void destroy(void *p)
{
    inst_t *in = p;
    pthread_mutex_lock(&shared_lock);
    if (--users == 0) {
        tp_core_stop(shared_core);
        shared_core = NULL;
    }
    pthread_mutex_unlock(&shared_lock);
    free(in);
}

static void midi(void *p, const uint8_t *msg, int len)
{
    (void)p, (void)msg, (void)len;
}

static void render(void *p, int16_t *out, int frames)
{
    (void)p;
    memset(out, 0, frames * 2 * sizeof *out);   /* the Torpedo's audio never goes through the MPC */
}

static const pp_param_t *find(const char *key, size_t len)
{
    for (int i = 0; i < PP_NPARAMS; i++)
        if (strlen(pp_params[i].key) == len && !strncmp(pp_params[i].key, key, len))
            return &pp_params[i];
    return NULL;
}

static int source_preset(const tp_state_t *s)   /* the preset the current sound comes from, edited or not */
{
    int n = s->setup[TP_S_SETUP_PRESET] == TP_EDIT_BUFFER ? s->setup[TP_S_SETUP_PRESET_SOURCE]
                                                           : s->setup[TP_S_SETUP_PRESET];
    return n >= 0 && n < TP_PRESETS ? n : 0;
}

static int value(const pp_param_t *pp, const tp_state_t *s)
{
    int v = 0;
    switch (pp->kind) {
    case PP_VIEW:
        v = s->params[TP_P_PRESET_MODE];
        return s->connected ? 1 + (v >= 0 && v <= TP_IR ? v : 0) : 0;
    case PP_PRESET: return 1 + source_preset(s);
    case PP_PARAM: v = s->params[pp->index]; break;
    case PP_SETUP: v = s->setup[pp->index]; break;
    }
    return v < 0 ? 0 : v;
}

static int text(inst_t *in, const pp_param_t *pp, const tp_state_t *s, char *buf, int len)
{
    int v = value(pp, s);
    char name[TP_NAME_LEN] = "";
    if (pp->kind == PP_PRESET) {
        int n = source_preset(s), edited = s->setup[TP_S_SETUP_PRESET] == TP_EDIT_BUFFER;
        tp_core_preset_name(in->core, n, name, sizeof name, NULL);
        return snprintf(buf, len, "A%03d%s %s", n + 1, edited ? "*" : "", name);
    }
    if (pp->eq_custom && s->params[TP_P_EQ_MODE] != 2) {
        int mode = s->params[TP_P_EQ_MODE] == 1;
        return snprintf(buf, len, "%s", pp->otherwise[mode]);
    }
    if (pp->list == PP_LIST_CAB || pp->list == PP_LIST_MIC) {
        tp_cab_t cab;
        int i = pp->list == PP_LIST_CAB ? v : s->params[TP_P_SIMUHP_SPEAKER];
        if (!tp_core_cab(in->core, i, &cab))
            snprintf(name, sizeof name, "%s", pp->list == PP_LIST_CAB ? cab.name : v < 8 ? cab.mics[v] : "");
        return snprintf(buf, len, "%s", name[0] ? name : "-");
    }
    if (pp->list == PP_LIST_IR) {
        tp_core_ir_name(in->core, v, name, sizeof name);
        return snprintf(buf, len, "%s", name[0] ? name : "-");
    }
    if (pp->text && v < pp->ntext)
        return snprintf(buf, len, "%s", pp->text[v]);
    return 0;   /* the wrapper's own text */
}

static int get_param(void *p, const char *key, char *buf, int len)
{
    inst_t *in = p;
    tp_state_t s;
    size_t n = strlen(key);
    if (!strcmp(key, "display_rev"))
        return snprintf(buf, len, "%u", tp_core_revision(in->core));
    const char *colon = strchr(key, ':');
    if (colon && find(key, colon - key) && find(key, colon - key)->kind == PP_PRESET) {   /* "preset:<n>": its name */
        char name[TP_NAME_LEN] = "";
        int k = atoi(colon + 1);
        if (k < 1 || k > TP_PRESETS || tp_core_preset_name(in->core, k - 1, name, sizeof name, NULL) || !name[0])
            return 0;
        return snprintf(buf, len, "A%03d %s", k, name);
    }
    int display = n > 8 && !strcmp(key + n - 8, "_display");
    const pp_param_t *pp = find(key, display ? n - 8 : n);
    if (!pp || pp->kind == PP_TRIGGER)
        return 0;   /* "state" included: nothing to save */
    tp_core_state(in->core, &s);
    if (display)
        return text(in, pp, &s, buf, len);
    return snprintf(buf, len, "%d", value(pp, &s));
}

static int clamp(int v, int hi)
{
    return v < 0 ? 0 : v > hi ? hi : v;
}

static void set_param(void *p, const char *key, const char *val)
{
    inst_t *in = p;
    const pp_param_t *pp = find(key, strlen(key));
    int v = atoi(val);
    if (!pp)
        return;
    switch (pp->kind) {   /* refused writes (arcade mode, not connected) leave the value as it was */
    case PP_PRESET: tp_core_select_preset(in->core, v - 1); break;
    case PP_SETUP: tp_core_set_setup(in->core, pp->index, v); break;
    case PP_PARAM:
        if (pp->list == PP_LIST_CAB)
            tp_core_select_cab(in->core, clamp(v, tp_core_cab_count(in->core) - 1));
        else if (pp->list == PP_LIST_IR)
            tp_core_select_ir(in->core, pp->index == TP_P_SIMUHP_WAVE1, clamp(v, tp_core_ir_count(in->core) - 1));
        else if (pp->list == PP_LIST_MIC) {
            tp_state_t s;
            tp_cab_t cab;
            tp_core_state(in->core, &s);
            if (v >= 0 && v < 8 && !tp_core_cab(in->core, s.params[TP_P_SIMUHP_SPEAKER], &cab) && cab.mics[v][0])
                tp_core_set_param(in->core, pp->index, v);   /* not an empty mic slot */
        } else
            tp_core_set_param(in->core, pp->index, v);
        break;
    }
}

const mpc_engine_t *mpc_engine(void)
{
    static const mpc_engine_t api = {create, destroy, midi, set_param, get_param, render, NULL, NULL};
    return &api;
}
