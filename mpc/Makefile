# The C side of the MPC plugin.
#   make host   build/probe for this machine, with ASan and UBSan (tests/test_mpc.py uses it)
#   make arm    build/arm/torpedo-probe for Gen1 MPCs (armv7), static, cross-built with Zig
ZIG ?= uvx --from ziglang==0.13.0 python -m ziglang
CFLAGS = -O2 -Wall -Wextra -std=gnu11 -Icore
SAN = -g -fsanitize=address,undefined -fno-sanitize-recover=all
CORE = core/tp_hid.c core/tp_dev.c core/tp_tables.c
DEPS = $(CORE) $(wildcard core/*.h)

.PHONY: host arm clean
host: build/probe
arm: build/arm/torpedo-probe

build/probe: probe/probe.c $(DEPS)
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) $(SAN) -o $@ probe/probe.c $(CORE)

# musl, static: no glibc version to match on the device (the plugin's .so will need glibc <= 2.31)
build/arm/torpedo-probe: probe/probe.c $(DEPS)
	@mkdir -p $(@D)
	$(ZIG) cc -target arm-linux-musleabihf -mcpu=cortex_a17 -static -s $(CFLAGS) -o $@ probe/probe.c $(CORE)

clean:
	rm -rf build
