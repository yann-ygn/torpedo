#define _GNU_SOURCE
#include "tp_hid.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "tp_tables.h"

int tp_hid_find(const char *sysfs, char *path, size_t len)
{
    char id[64], file[512], line[256];
    const char *name = "HID_NAME=" TP_MANUFACTURER;
    snprintf(id, sizeof id, "HID_ID=0003:%08X:%08X", TP_VID, TP_PID);   /* 0003 = USB */
    DIR *dir = opendir(sysfs);
    if (!dir)
        return -1;
    int found = 0;
    struct dirent *e;
    while (!found && (e = readdir(dir))) {
        if (e->d_name[0] == '.')
            continue;
        snprintf(file, sizeof file, "%s/%s/device/uevent", sysfs, e->d_name);
        FILE *f = fopen(file, "r");
        if (!f)
            continue;
        int id_ok = 0, name_ok = 0;
        while (fgets(line, sizeof line, f)) {
            line[strcspn(line, "\n")] = 0;
            id_ok |= !strcmp(line, id);
            name_ok |= !strncmp(line, name, strlen(name));
        }
        fclose(f);
        if (id_ok && name_ok) {
            snprintf(path, len, "/dev/%s", e->d_name);
            found = 1;
        }
    }
    closedir(dir);
    if (!found)
        errno = ENODEV;
    return found ? 0 : -1;
}

int tp_hid_open(const char *path)
{
    if (strncmp(path, "unix:", 5))
        return open(path, O_RDWR | O_CLOEXEC);
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    if (strlen(path + 5) >= sizeof a.sun_path) {
        errno = ENAMETOOLONG;
        return -1;
    }
    strcpy(a.sun_path, path + 5);
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd >= 0 && connect(fd, (struct sockaddr *)&a, sizeof a) < 0) {
        int err = errno;
        close(fd);
        errno = err;
        return -1;
    }
    return fd;
}

int tp_hid_write(int fd, const uint8_t *payload, int n)
{
    uint8_t buf[1 + TP_REPORT_LEN] = {0};   /* report ID 0: the device has no numbered reports */
    if (n < 0 || n > TP_REPORT_LEN) {
        errno = EINVAL;
        return -1;
    }
    memcpy(buf + 1, payload, n);
    ssize_t k = write(fd, buf, sizeof buf);
    if (k == (ssize_t)sizeof buf)
        return 0;
    if (k >= 0)
        errno = EIO;
    return -1;
}

int tp_hid_read(int fd, uint8_t *report, int timeout_ms)
{
    struct pollfd p = {.fd = fd, .events = POLLIN};
    int r = poll(&p, 1, timeout_ms);
    if (r < 0)
        return errno == EINTR ? 0 : -1;
    if (!r)
        return 0;
    if (!(p.revents & POLLIN)) {   /* POLLHUP/POLLERR: unplugged, or the bridge went away */
        errno = ENODEV;
        return -1;
    }
    ssize_t k = read(fd, report, TP_REPORT_LEN);
    if (k <= 0) {
        if (!k)
            errno = ENODEV;
        return -1;
    }
    memset(report + k, 0, TP_REPORT_LEN - k);
    return 1;
}
