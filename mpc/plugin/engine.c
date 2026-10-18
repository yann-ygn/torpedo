/* The Torpedo Live as an MPC plugin engine (mpc-vst-plugins wrapper/engine.h): a silent instrument whose
 * parameters are the unit's, through one tp_core shared by every instance in the process (two cores would
 * fight over the device). Parameters are listed in params.json, generated with plugin_tables.c by
 * gen_plugin.py.
 *
 * - Values read are the core's (the device's with queued writes on top); writes are queued, never waited on.
 * - "display_rev" is the core's revision plus this instance's own (the PRESET page's state), so the wrapper
 *   (HAS_DISPLAY_REV) reports what changed by itself (front panel, linked parameters, preset loads, the
 *   connection, a question timing out) and redraws texts and when= panels.
 * - A write to a parameter the preset's mode doesn't show (pp_param_t.modes, from layout.py) is dropped: a tab's
 *   Q-Links stay on its controls whatever the mode, and in simulation or IR mode the unit would take it, an edit
 *   nobody hears.
 * - No "state": nothing is saved with the project or pushed to the unit when one loads.
 * - TORPEDO_DEVICE in the environment overrides the device path (tests: unix:<socket>).
 *
 * The PRESET page: 16 tiles show a bank of presets (lit: the current one), a tap loads one. Anything that loses
 * or overwrites something is a question first, answered with CONFIRM or CANCEL: loading over unsaved edits,
 * discarding them, saving (SAVE: into the preset they came from; SAVE AS: tap the preset to save into, the empty
 * ones lit), renaming. A save question can change the name first (NAME...: a keyboard replaces the tiles; OK goes
 * back to the question), and RENAME (no unsaved edits) starts with the keyboard. Saving is the plugin's only flash
 * write (tp_core_store). A question is dropped after a while, or when the unit changes under it (another preset,
 * an edit, a store from another instance), since it was asked about what the screen showed then. */
#define _GNU_SOURCE
#include <limits.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "engine.h"
#include "plugin_tables.h"
#include "tp_core.h"

#define BANK_SIZE 16           /* tiles on the PRESET page (gen_plugin.py TILES) */
#define ASK_TIMEOUT_MS 20000   /* an unanswered question goes away */
#define NAME_TIMEOUT_MS 60000  /* the keyboard: from the last key */
#define NOTICE_MS 6000         /* how long the outcome of a store stays up */
#define EMPTY_PRESET "USER"    /* the name of an empty slot (protocol.py EMPTY_PRESET) */

enum { ACT_SAVE, ACT_SAVEAS, ACT_DISCARD, ACT_CONFIRM, ACT_CANCEL, ACT_RENAME, ACT_NAME };   /* gen_plugin.py ACTIONS */
enum { PUI_CLEAN, PUI_EDITED, PUI_PICK, PUI_CONFIRM, PUI_SAVING, PUI_NAMING, PUI_ASKSAVE };   /* gen_plugin.py PUI */
enum { KEY_DEL = 0x08, KEY_CLEAR = 0x18 };   /* gen_plugin.py KEY_*; a character's key is its code */
enum { ASK_NONE, ASK_PICK, ASK_SAVE, ASK_LOAD, ASK_DISCARD, ASK_RENAME, ASK_NAME };

static pthread_mutex_t shared_lock = PTHREAD_MUTEX_INITIALIZER;
static tp_core_t *shared_core;
static int users;

typedef struct {
    tp_core_t *core;
    unsigned rev;                 /* +1 whenever the PRESET page's own state changes */
    int bank;                     /* the bank of presets the tiles show */
    int seen_source;              /* the preset the sound came from, last seen: the bank follows it */
    int ask, target;              /* the open question (ASK_*) and the preset it is about */
    int ask_source, ask_edited;   /* what the unit had when it was asked */
    long long ask_until;
    int back;                     /* ASK_NAME: the question OK goes back to (ASK_SAVE, ASK_RENAME) */
    char name[TP_NAME_MAX + 1];   /* ASK_NAME: the name being typed */
    char save_name[TP_NAME_MAX + 1];   /* the name the save or rename question stores under; "": the buffer's */
    unsigned seen_store;          /* tp_state_t.store_seq already reported (UINT_MAX: not looked yet) */
    char notice[2][80];           /* the outcome of the last store, until notice_until */
    long long notice_until;
} inst_t;

static long long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000LL + t.tv_nsec / 1000000;
}

static void *create(const char *data_dir)
{
    (void)data_dir;
    inst_t *in = calloc(1, sizeof *in);
    if (!in)
        return NULL;
    in->seen_source = -1;
    in->seen_store = UINT_MAX;
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

static int edited(const tp_state_t *s)
{
    return s->setup[TP_S_SETUP_PRESET] == TP_EDIT_BUFFER;
}

static char mode_letter(int mode)   /* what the unit's panel puts before a preset's number */
{
    return mode == TP_ARCADE ? 'A' : mode == TP_IR ? 'I' : 'S';
}

typedef struct { char s[8]; } label_t;

/* Preset n as the unit's panel shows it: the letter of the mode it is stored in (the name list's byte), then its
 * number (A021 in simulation is S021). */
static label_t label(inst_t *in, int n)
{
    label_t l;
    int mode = 0;
    tp_core_preset_name(in->core, n, NULL, 0, &mode);
    snprintf(l.s, sizeof l.s, "%c%03d", mode_letter(mode), n + 1);
    return l;
}

/* --- the PRESET page ------------------------------------------------------------------------------------------- */

static void preset_name(inst_t *in, int n, char *buf, int len)
{
    buf[0] = 0;
    tp_core_preset_name(in->core, n, buf, len, NULL);
}

static int is_empty(inst_t *in, int n)
{
    char name[TP_NAME_LEN];
    preset_name(in, n, name, sizeof name);
    return !strcmp(name, EMPTY_PRESET);
}

/* The name a save question stores under: the one typed, else the edit buffer's own. */
static void save_name(inst_t *in, char *buf, int len)
{
    if (in->save_name[0])
        snprintf(buf, len, "%s", in->save_name);
    else
        preset_name(in, TP_EDIT_BUFFER, buf, len);
}

static void set_ask(inst_t *in, int ask)
{
    if (in->ask != ask)
        in->rev++;
    in->ask = ask;
}

/* Open a question about `target`, asked while the unit was as `s` says. */
static void ask(inst_t *in, const tp_state_t *s, int what, int target)
{
    set_ask(in, what);
    in->target = target;
    in->ask_source = source_preset(s);
    in->ask_edited = edited(s);
    in->ask_until = now_ms() + (what == ASK_NAME ? NAME_TIMEOUT_MS : ASK_TIMEOUT_MS);
    in->notice_until = 0;
}

/* The keyboard, filled with `name`, for the question `back` (OK returns to it with the name typed). */
static void ask_name(inst_t *in, const tp_state_t *s, int back, const char *name)
{
    in->back = back;
    snprintf(in->name, sizeof in->name, "%s", name);
    ask(in, s, ASK_NAME, in->target);
}

static void notice(inst_t *in, const char *line1, const char *line2)
{
    snprintf(in->notice[0], sizeof in->notice[0], "%s", line1);
    snprintf(in->notice[1], sizeof in->notice[1], "%s", line2);
    in->notice_until = now_ms() + NOTICE_MS;
}

/* What happened since the page was last looked at: a store ended, the question went stale, another preset came
 * (the tiles follow it to its bank). Called before anything is read or done. */
static void tick(inst_t *in, const tp_state_t *s)
{
    int src = source_preset(s);
    if (in->seen_store == UINT_MAX)
        in->seen_store = s->store_seq;   /* earlier stores (another instance's, before this one) aren't news */
    else if (s->store_seq != in->seen_store) {
        char name[TP_NAME_LEN], line[80];
        in->seen_store = s->store_seq;
        preset_name(in, s->store_slot, name, sizeof name);
        if (s->store_status == TP_STORE_OK) {
            snprintf(line, sizeof line, "SAVED %s", label(in, s->store_slot).s);
            notice(in, line, name);
        } else {
            snprintf(line, sizeof line, "%s: SAVE NOT CONFIRMED", label(in, s->store_slot).s);
            notice(in, line, "CHECK THE PRESET ON THE UNIT");
        }
        in->rev++;
    }
    if (in->ask != ASK_NONE) {
        int changed = src != in->ask_source || edited(s) != in->ask_edited || s->storing >= 0;
        if (!s->connected || changed || now_ms() > in->ask_until) {
            if (s->connected && changed && s->storing < 0)
                notice(in, "CANCELLED", "THE UNIT CHANGED MEANWHILE");
            set_ask(in, ASK_NONE);
        }
    }
    if (s->connected && src != in->seen_source) {
        in->seen_source = src;
        if (in->bank != src / BANK_SIZE) {
            in->bank = src / BANK_SIZE;
            in->rev++;
        }
    }
}

static int pui(const inst_t *in, const tp_state_t *s)
{
    if (s->storing >= 0)
        return PUI_SAVING;
    switch (in->ask) {
    case ASK_NONE: return edited(s) ? PUI_EDITED : PUI_CLEAN;
    case ASK_PICK: return PUI_PICK;
    case ASK_NAME: return PUI_NAMING;
    case ASK_SAVE:
    case ASK_RENAME: return PUI_ASKSAVE;
    default: return PUI_CONFIRM;
    }
}

static int tile_on(inst_t *in, const tp_state_t *s, int tile)
{
    int n = in->bank * BANK_SIZE + tile;
    if (!s->connected)
        return 0;
    switch (pui(in, s)) {
    case PUI_CLEAN:
    case PUI_EDITED: return n == source_preset(s);
    case PUI_PICK: return is_empty(in, n);   /* where the sound can go without replacing anything */
    case PUI_SAVING: return n == s->storing;
    default: return n == in->target;         /* the preset the question is about */
    }
}

static int tile_text(inst_t *in, const tp_state_t *s, int tile, char *buf, int len)
{
    char name[TP_NAME_LEN];
    int n = in->bank * BANK_SIZE + tile;
    if (!s->connected)
        return snprintf(buf, len, "-");
    preset_name(in, n, name, sizeof name);
    return snprintf(buf, len, "%s %s", label(in, n).s, name);
}

/* Line 0 and 1: the page's two message lines; line 2: the name being typed, with its cursor. */
static int message(inst_t *in, const tp_state_t *s, int line, char *buf, int len)
{
    char l[3][80] = {"", "", ""}, name[TP_NAME_LEN];
    int src = source_preset(s), p = pui(in, s);
    if (!s->connected)
        snprintf(l[0], sizeof l[0], "TORPEDO NOT CONNECTED");
    else switch (p) {
    case PUI_SAVING:
        snprintf(l[0], sizeof l[0], "SAVING %s...", label(in, s->storing).s);
        snprintf(l[1], sizeof l[1], "KEEP THE TORPEDO PLUGGED IN");
        break;
    case PUI_PICK:
        snprintf(l[0], sizeof l[0], "SAVE INTO WHICH PRESET?");
        snprintf(l[1], sizeof l[1], "TAP IT (THE LIT ONES ARE EMPTY)");
        break;
    case PUI_NAMING:
        snprintf(l[0], sizeof l[0], "NAME FOR %s", label(in, in->target).s);
        snprintf(l[1], sizeof l[1], "%s", strlen(in->name) < TP_NAME_MAX ? "UP TO 25 LETTERS, DIGITS, SPACES"
                                                                          : "25 CHARACTERS: AS LONG AS IT GETS");
        snprintf(l[2], sizeof l[2], "%s%s", in->name, strlen(in->name) < TP_NAME_MAX ? "_" : "");
        break;
    case PUI_ASKSAVE:
        save_name(in, name, sizeof name);
        if (in->ask == ASK_RENAME) {
            snprintf(l[0], sizeof l[0], "RENAME %s?", label(in, in->target).s);
            snprintf(l[1], sizeof l[1], "TO %s", name);
            break;
        }
        snprintf(l[1], sizeof l[1], "AS %s", name);
        if (in->target == src && edited(s))
            snprintf(l[0], sizeof l[0], "SAVE THE EDITS INTO %s?", label(in, in->target).s);
        else if (is_empty(in, in->target))
            snprintf(l[0], sizeof l[0], "SAVE INTO %s (EMPTY)?", label(in, in->target).s);
        else {
            preset_name(in, in->target, name, sizeof name);
            snprintf(l[0], sizeof l[0], "REPLACE %s %s?", label(in, in->target).s, name);
        }
        break;
    case PUI_CONFIRM:
        if (in->ask == ASK_LOAD) {
            snprintf(l[0], sizeof l[0], "LOAD %s?", label(in, in->target).s);
            snprintf(l[1], sizeof l[1], "THE EDITS TO %s ARE DROPPED", label(in, src).s);
        } else {
            snprintf(l[0], sizeof l[0], "DISCARD THE EDITS?");
            snprintf(l[1], sizeof l[1], "%s RELOADS AS STORED", label(in, src).s);
        }
        break;
    default:
        if (now_ms() < in->notice_until)
            memcpy(l, in->notice, sizeof in->notice);
        else if (edited(s)) {
            snprintf(l[0], sizeof l[0], "%s HAS UNSAVED EDITS", label(in, src).s);
            snprintf(l[1], sizeof l[1], "SAVE, SAVE AS OR DISCARD THEM");
        } else {
            snprintf(l[0], sizeof l[0], "TAP A PRESET TO LOAD IT");
            snprintf(l[1], sizeof l[1], "SAVE AS COPIES IT TO ANOTHER ONE");
        }
    }
    return snprintf(buf, len, "%s", l[line]);
}

/* A tile tapped: preset n. */
static void tap(inst_t *in, const tp_state_t *s, int n)
{
    if (!s->connected || s->storing >= 0 || n < 0 || n >= TP_PRESETS)
        return;
    if (in->ask == ASK_PICK) {
        ask(in, s, ASK_SAVE, n);
        return;
    }
    if (in->ask != ASK_NONE)
        return;   /* answer the question first */
    if (edited(s))
        ask(in, s, ASK_LOAD, n);
    else if (n != source_preset(s))
        tp_core_select_preset(in->core, n);
}

/* A key of the name keyboard: a character goes at the end (no leading or double spaces), DEL takes the last one off. */
static void type(inst_t *in, int key)
{
    size_t n = strlen(in->name);
    if (in->ask != ASK_NAME)
        return;
    if (key == KEY_DEL && n)
        in->name[n - 1] = 0;
    else if (key == KEY_CLEAR)
        in->name[0] = 0;
    else if (key > ' ' && strchr(TP_NAME_CHARS, key) && n < TP_NAME_MAX)
        in->name[n] = key, in->name[n + 1] = 0;
    else if (key == ' ' && n && n < TP_NAME_MAX && in->name[n - 1] != ' ')
        in->name[n] = ' ', in->name[n + 1] = 0;
    in->ask_until = now_ms() + NAME_TIMEOUT_MS;
}

static void act(inst_t *in, const tp_state_t *s, int action)
{
    char name[TP_NAME_LEN];
    int src = source_preset(s);
    size_t n;
    if (!s->connected || s->storing >= 0)
        return;
    switch (action) {
    case ACT_SAVE:
        if (in->ask == ASK_NONE && edited(s)) {
            in->save_name[0] = 0;
            ask(in, s, ASK_SAVE, src);
        }
        break;
    case ACT_SAVEAS:
        if (in->ask == ASK_NONE) {
            in->save_name[0] = 0;
            ask(in, s, ASK_PICK, src);
        }
        break;
    case ACT_DISCARD:
        if (in->ask == ASK_NONE && edited(s))
            ask(in, s, ASK_DISCARD, src);
        break;
    case ACT_RENAME:   /* with edits, renaming would store them too: SAVE or SAVE AS, then NAME..., is that */
        if (in->ask == ASK_NONE && !edited(s)) {
            in->save_name[0] = 0;
            in->target = src;
            preset_name(in, src, name, sizeof name);
            ask_name(in, s, ASK_RENAME, name);
        }
        break;
    case ACT_NAME:
        if (in->ask == ASK_SAVE || in->ask == ASK_RENAME) {
            save_name(in, name, sizeof name);
            ask_name(in, s, in->ask, name);
        }
        break;
    case ACT_CANCEL:
        if (in->ask == ASK_NAME && (in->back == ASK_SAVE || in->save_name[0]))
            ask(in, s, in->back, in->target);   /* back to the question, with the name it had */
        else
            set_ask(in, ASK_NONE);
        break;
    case ACT_CONFIRM:
        switch (in->ask) {
        case ASK_NAME:   /* OK */
            for (n = strlen(in->name); n && in->name[n - 1] == ' '; n--)
                in->name[n - 1] = 0;
            if (!n)
                return;   /* no name yet */
            snprintf(in->save_name, sizeof in->save_name, "%s", in->name);
            ask(in, s, in->back, in->target);
            return;
        case ASK_SAVE:
        case ASK_RENAME: tp_core_store(in->core, in->target, in->save_name); break;
        case ASK_LOAD:
        case ASK_DISCARD: tp_core_select_preset(in->core, in->target); break;
        default: return;
        }
        set_ask(in, ASK_NONE);
        break;
    }
}

/* --- parameters -------------------------------------------------------------------------------------------------- */

static int value(const inst_t *in, const pp_param_t *pp, const tp_state_t *s)
{
    int v = 0;
    switch (pp->kind) {
    case PP_VIEW:
        v = s->params[TP_P_PRESET_MODE];
        return s->connected ? 1 + (v >= 0 && v <= TP_IR ? v : 0) : 0;
    case PP_PRESET: return 1 + source_preset(s);
    case PP_PARAM: v = s->params[pp->index]; break;
    case PP_SETUP: v = s->setup[pp->index]; break;
    case PP_BANK: return in->bank;
    case PP_PUI: return pp->index ? pui(in, s) == PUI_NAMING : pui(in, s);   /* 1: the name keyboard shows */
    }
    return v < 0 ? 0 : v;
}

static int text(inst_t *in, const pp_param_t *pp, const tp_state_t *s, char *buf, int len)
{
    int v = value(in, pp, s);
    char name[TP_NAME_LEN] = "";
    if (pp->kind == PP_PRESET) {   /* the sound's own mode: an edit switched to IR is I..., unsaved */
        int n = source_preset(s);
        tp_core_preset_name(in->core, n, name, sizeof name, NULL);
        return snprintf(buf, len, "%c%03d%s %s", mode_letter(s->params[TP_P_PRESET_MODE]), n + 1,
                        edited(s) ? "*" : "", name);
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
    if (!strcmp(key, "display_rev")) {
        tp_core_state(in->core, &s);
        tick(in, &s);
        return snprintf(buf, len, "%u.%u", tp_core_revision(in->core), in->rev);
    }
    const char *colon = strchr(key, ':');
    if (colon && find(key, colon - key) && find(key, colon - key)->kind == PP_PRESET) {   /* "preset:<n>": its name */
        char name[TP_NAME_LEN] = "";
        int k = atoi(colon + 1);
        if (k < 1 || k > TP_PRESETS || tp_core_preset_name(in->core, k - 1, name, sizeof name, NULL) || !name[0])
            return 0;
        return snprintf(buf, len, "%s %s", label(in, k - 1).s, name);
    }
    int display = n > 8 && !strcmp(key + n - 8, "_display");
    int on = n > 3 && !strcmp(key + n - 3, "_on");   /* a list tile: lit or not */
    const pp_param_t *pp = find(key, display ? n - 8 : on ? n - 3 : n);
    if (!pp || pp->kind == PP_TRIGGER || pp->kind == PP_ACTION || pp->kind == PP_KEY || (on && pp->kind != PP_SLOT))
        return 0;   /* "state" included: nothing to save */
    tp_core_state(in->core, &s);
    tick(in, &s);
    if (pp->kind == PP_SLOT)
        return on ? snprintf(buf, len, "%d", tile_on(in, &s, pp->index)) : tile_text(in, &s, pp->index, buf, len);
    if (pp->kind == PP_MSG)
        return message(in, &s, pp->index, buf, len);
    if (display)
        return text(in, pp, &s, buf, len);
    return snprintf(buf, len, "%d", value(in, pp, &s));
}

static int clamp(int v, int hi)
{
    return v < 0 ? 0 : v > hi ? hi : v;
}

static void set_param(void *p, const char *key, const char *val)
{
    inst_t *in = p;
    const pp_param_t *pp = find(key, strlen(key));
    tp_state_t s;
    int v = atoi(val), mode;
    if (!pp)
        return;
    switch (pp->kind) {   /* refused writes (another mode's, not connected) leave the value as it was */
    case PP_PRESET: tp_core_select_preset(in->core, v - 1); break;
    case PP_SETUP: tp_core_set_setup(in->core, pp->index, v); break;
    case PP_PARAM:
        tp_core_state(in->core, &s);
        mode = s.params[TP_P_PRESET_MODE];
        if (mode < 0 || mode > TP_IR || !(pp->modes & 1 << mode))
            break;
        if (pp->list == PP_LIST_CAB)
            tp_core_select_cab(in->core, clamp(v, tp_core_cab_count(in->core) - 1));
        else if (pp->list == PP_LIST_IR)
            tp_core_select_ir(in->core, pp->index == TP_P_SIMUHP_WAVE1, clamp(v, tp_core_ir_count(in->core) - 1));
        else if (pp->list == PP_LIST_MIC) {
            tp_cab_t cab;
            if (v >= 0 && v < 8 && !tp_core_cab(in->core, s.params[TP_P_SIMUHP_SPEAKER], &cab) && cab.mics[v][0])
                tp_core_set_param(in->core, pp->index, v);   /* not an empty mic slot */
        } else
            tp_core_set_param(in->core, pp->index, v);
        break;
    case PP_SLOT:   /* a tap: "1", or "0" on a lit tile (the wrapper drops the echoes of what it pushed itself) */
        tp_core_state(in->core, &s);
        tick(in, &s);
        tap(in, &s, in->bank * BANK_SIZE + pp->index);
        break;
    case PP_BANK:
        if (v >= 0 && v < TP_PRESETS / BANK_SIZE && v != in->bank) {
            in->bank = v;
            in->rev++;
        }
        break;
    case PP_ACTION:
    case PP_KEY:
        if (v) {   /* not the trigger falling back to 0 */
            tp_core_state(in->core, &s);
            tick(in, &s);
            if (pp->kind == PP_KEY)
                type(in, pp->index);
            else
                act(in, &s, pp->index);
        }
        break;
    }
}

const mpc_engine_t *mpc_engine(void)
{
    static const mpc_engine_t api = {create, destroy, midi, set_param, get_param, render, NULL, NULL};
    return &api;
}
