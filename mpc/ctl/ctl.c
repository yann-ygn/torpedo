/* torpedo-ctl: drives tp_core from stdin, one command per line (tests/test_mpc_core.py uses it; it
 * works by hand over SSH too). Changes the device makes on its own are printed as they come
 * ("changed ..."). Each command ends with "ok", "end" (after a listing) or "error <reason>".
 *   state | meters | cabs | irs | names
 *   set NAME V | setup NAME V | preset N | cab N | ir SLOT N     queue a change
 *   store N [NAME]       queue storing the current sound into preset N (a flash write), renamed NAME (the rest of
 *                        the line) if given; `storing` prints how it stands
 *   clear N              queue emptying preset N (a flash write), the same way
 *   sweep NAME FROM TO   set FROM..TO as fast as possible, from a thread of its own
 *   sync [MS]            wait until the queued changes are handled
 *   connected [MS]       wait for the connection
 *   sleep MS | quit
 * Options: --poll-ms N, --retry-ms N, PATH (default: found in sysfs; unix:<socket> for the tests). */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "tp_core.h"

static tp_core_t *core;
static pthread_mutex_t out_lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_int quitting;

static void out(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    pthread_mutex_lock(&out_lock);
    vprintf(fmt, ap);
    fflush(stdout);
    pthread_mutex_unlock(&out_lock);
    va_end(ap);
}

static int param_index(const char *name)
{
    for (int i = 0; i < TP_NPARAMS; i++)
        if (!strcasecmp(name, tp_params[i].name))
            return i;
    return -1;
}

static int setup_index(const char *name)
{
    for (int i = 0; i < TP_NSETUP; i++)
        if (!strcasecmp(name, tp_setup_names[i]))
            return i;
    return -1;
}

static void *watch(void *arg)
{
    (void)arg;
    while (!quitting) {
        tp_changes_t ch;
        tp_state_t s;
        tp_core_take_changes(core, &ch);
        tp_core_state(core, &s);
        if (ch.flags & TP_CHANGED_CONNECTION)
            out("changed connection %d\n", s.connected);
        if (s.connected) {
            for (int i = 0; i < TP_NPARAMS; i++)
                if (ch.params >> i & 1)
                    out("changed param %s %d\n", tp_params[i].name, s.params[i]);
            for (int i = 0; i < TP_NSETUP; i++)
                if (ch.setup >> i & 1)
                    out("changed setup %s %d\n", tp_setup_names[i], s.setup[i]);
        }
        if (ch.flags & TP_CHANGED_NAMES)
            out("changed names\n");
        if (ch.flags & TP_CHANGED_CABS)
            out("changed cabs\n");
        if (ch.flags & TP_CHANGED_IRS)
            out("changed irs\n");
        usleep(10000);
    }
    return NULL;
}

struct sweep {
    int index, from, to, errors;
};

static void *sweeper(void *arg)
{
    struct sweep *s = arg;
    int step = s->from <= s->to ? 1 : -1;
    for (int v = s->from;; v += step) {
        s->errors += tp_core_set_param(core, s->index, v) < 0;
        if (v == s->to)
            break;
    }
    return NULL;
}

static void result(int r)
{
    if (r < 0)
        out("error %s\n", strerror(errno));
    else
        out("ok\n");
}

static int wait_connected(int ms)
{
    tp_state_t s;
    for (;; ms -= 10) {
        tp_core_state(core, &s);
        if (s.connected)
            return 0;
        if (ms <= 0) {
            errno = ETIMEDOUT;
            return -1;
        }
        usleep(10000);
    }
}

static void print_state(void)
{
    tp_state_t s;
    tp_core_state(core, &s);
    out("connected %d generation %u serial %s firmware %s\n", s.connected, s.generation, s.serial, s.firmware);
    for (int i = 0; i < TP_NPARAMS; i++)
        out("param %s %d\n", tp_params[i].name, s.params[i]);
    for (int i = 0; i < TP_NSETUP; i++)
        out("setup %s %d\n", tp_setup_names[i], s.setup[i]);
    out("end\n");
}

/* The text after the first k words of s, without the line end ("" if none). */
static char *after_words(char *s, int k)
{
    s += strspn(s, " \t");
    for (; k > 0; k--) {
        s += strcspn(s, " \t\r\n");
        s += strspn(s, " \t");
    }
    s[strcspn(s, "\r\n")] = 0;
    return s;
}

static void command(char *line)
{
    char raw[256];
    snprintf(raw, sizeof raw, "%s", line);
    char *w[5] = {0};
    int n = 0;
    for (char *t = strtok(line, " \t\r\n"); t && n < 5; t = strtok(NULL, " \t\r\n"))
        w[n++] = t;
    if (!n)
        return;
    const char *cmd = w[0];
    int a1 = n > 1 ? atoi(w[1]) : 0, a2 = n > 2 ? atoi(w[2]) : 0;
    if (!strcmp(cmd, "state"))
        print_state();
    else if (!strcmp(cmd, "meters")) {
        tp_state_t s;
        tp_core_state(core, &s);
        out("meters in %d out %d clip %d\nend\n", s.meter_in, s.meter_out, s.clip);
    } else if (!strcmp(cmd, "cabs")) {
        tp_cab_t cab;
        for (int i = 0; !tp_core_cab(core, i, &cab); i++) {
            char mics[8 * (TP_MIC_SLOT + 1)] = "";   /* one out() per line: the watcher prints too */
            for (int m = 0; m < 8; m++) {
                strcat(mics, cab.mics[m]);
                strcat(mics, m < 7 ? "," : "");
            }
            out("cab %d %s|%s|%s\n", i, cab.name, cab.maker, mics);
        }
        out("end\n");
    } else if (!strcmp(cmd, "irs")) {
        char name[TP_NAME_LEN];
        for (int i = 0; !tp_core_ir_name(core, i, name, sizeof name); i++)
            out("ir %d %s\n", i, name);
        out("end\n");
    } else if (!strcmp(cmd, "names")) {
        char name[TP_NAME_LEN];
        int mode;
        for (int i = 0; !tp_core_preset_name(core, i, name, sizeof name, &mode); i++)
            if (i < TP_PRESETS || i == TP_EDIT_BUFFER)
                out("name %d %d %s\n", i, mode, name);
        out("end\n");
    } else if (!strcmp(cmd, "storing")) {
        tp_state_t s;
        tp_core_state(core, &s);
        out("storing %d clearing %d status %d slot %d cleared %d seq %u\nend\n", s.storing, s.clearing,
            s.store_status, s.store_slot, s.store_cleared, s.store_seq);
    } else if (!strcmp(cmd, "set") && n == 3) {
        int i = param_index(w[1]);
        if (i < 0)
            out("error unknown parameter %s\n", w[1]);
        else
            result(tp_core_set_param(core, i, a2));
    } else if (!strcmp(cmd, "setup") && n == 3) {
        int i = setup_index(w[1]);
        if (i < 0)
            out("error unknown setting %s\n", w[1]);
        else
            result(tp_core_set_setup(core, i, a2));
    } else if (!strcmp(cmd, "preset") && n == 2)
        result(tp_core_select_preset(core, a1));
    else if (!strcmp(cmd, "cab") && n == 2)
        result(tp_core_select_cab(core, a1));
    else if (!strcmp(cmd, "ir") && n == 3)
        result(tp_core_select_ir(core, a1, a2));
    else if (!strcmp(cmd, "store") && n >= 2)
        result(tp_core_store(core, a1, after_words(raw, 2)));
    else if (!strcmp(cmd, "clear") && n == 2)
        result(tp_core_clear(core, a1));
    else if (!strcmp(cmd, "sweep") && n == 4) {
        struct sweep s = {param_index(w[1]), a2, atoi(w[3]), 0};
        pthread_t t;
        if (s.index < 0) {
            out("error unknown parameter %s\n", w[1]);
            return;
        }
        pthread_create(&t, NULL, sweeper, &s);
        pthread_join(t, NULL);
        if (s.errors)
            out("error %d writes refused\n", s.errors);
        else
            out("ok\n");
    } else if (!strcmp(cmd, "sync"))
        result(tp_core_wait_idle(core, n > 1 ? a1 : 5000));
    else if (!strcmp(cmd, "connected"))
        result(wait_connected(n > 1 ? a1 : 5000));
    else if (!strcmp(cmd, "sleep") && n == 2) {
        usleep(a1 * 1000);
        out("ok\n");
    } else
        out("error unknown command %s\n", cmd);
}

int main(int argc, char **argv)
{
    tp_core_config_t cfg = {0};
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--poll-ms") && i + 1 < argc)
            cfg.poll_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--retry-ms") && i + 1 < argc)
            cfg.retry_ms = atoi(argv[++i]);
        else if (argv[i][0] != '-' && !cfg.path)
            cfg.path = argv[i];
        else {
            fputs("usage: torpedo-ctl [--poll-ms N] [--retry-ms N] [PATH]\n", stderr);
            return 2;
        }
    }
    core = tp_core_start(&cfg);
    if (!core) {
        fprintf(stderr, "torpedo-ctl: %s\n", strerror(errno));
        return 1;
    }
    pthread_t watcher;
    pthread_create(&watcher, NULL, watch, NULL);
    char line[256];
    while (fgets(line, sizeof line, stdin) && strncmp(line, "quit", 4))
        command(line);
    quitting = 1;
    pthread_join(watcher, NULL);
    tp_core_stop(core);
    return 0;
}
