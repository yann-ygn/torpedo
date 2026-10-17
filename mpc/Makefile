# The C side of the MPC plugin.
#   make host   build/probe and build/ctl for this machine, with ASan and UBSan (tests/test_mpc*.py)
#   make tsan   build/ctl-tsan, with ThreadSanitizer (tests/test_mpc_core.py)
#   make arm    build/arm/torpedo-probe and torpedo-ctl for Gen1 MPCs (armv7), static, built with Zig
ZIG ?= uvx --from ziglang==0.13.0 python -m ziglang
CFLAGS = -O2 -Wall -Wextra -std=gnu11 -Icore
SAN = -g -fsanitize=address,undefined -fno-sanitize-recover=all
CORE = core/tp_hid.c core/tp_dev.c core/tp_core.c core/tp_tables.c
DEPS = $(CORE) $(wildcard core/*.h)
ARM = $(ZIG) cc -target arm-linux-musleabihf -mcpu=cortex_a17 -static -s

.PHONY: host tsan arm clean
host: build/probe build/ctl
tsan: build/ctl-tsan
arm: build/arm/torpedo-probe build/arm/torpedo-ctl

build/probe: probe/probe.c $(DEPS)
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) $(SAN) -o $@ probe/probe.c $(CORE)

build/ctl: ctl/ctl.c $(DEPS)
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) $(SAN) -o $@ ctl/ctl.c $(CORE) -lpthread

build/ctl-tsan: ctl/ctl.c $(DEPS)
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) -g -fsanitize=thread -o $@ ctl/ctl.c $(CORE) -lpthread

# musl, static: no glibc version to match on the device (the plugin's .so will need glibc <= 2.31)
build/arm/torpedo-probe: probe/probe.c $(DEPS)
	@mkdir -p $(@D)
	$(ARM) $(CFLAGS) -o $@ probe/probe.c $(CORE)

build/arm/torpedo-ctl: ctl/ctl.c $(DEPS)
	@mkdir -p $(@D)
	$(ARM) $(CFLAGS) -o $@ ctl/ctl.c $(CORE)

clean:
	rm -rf build
