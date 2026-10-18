# The C side of the MPC plugin.
#   make host   build/probe and build/ctl for this machine, with ASan and UBSan (tests/test_mpc*.py)
#   make tsan   build/ctl-tsan, with ThreadSanitizer (tests/test_mpc_core.py)
#   make arm    build/arm/torpedo-probe and torpedo-ctl for Gen1 MPCs (armv7), static, built with Zig
#   make plugin-host  build/plugin-host: the plugin in a stand-in host, with ASan (tests/test_mpc_plugin.py)
#   make plugin       plugin/build/plugin/ygn - VST - Torpedo Control/: the plugin folder for the MPC (armv7,
#                     glibc <= 2.31), with its skin (mpc-vst-plugins' generator, drawn by the browser renderer on the
#                     system Chromium: plugin/html_art_local; needs `git submodule update --init`)
ZIG ?= uvx --from ziglang==0.13.0 python -m ziglang
CFLAGS = -O2 -Wall -Wextra -std=gnu11 -Icore
SAN = -g -fsanitize=address,undefined -fno-sanitize-recover=all
CORE = core/tp_hid.c core/tp_dev.c core/tp_core.c core/tp_tables.c
DEPS = $(CORE) $(wildcard core/*.h)
ARM = $(ZIG) cc -target arm-linux-musleabihf -mcpu=cortex_a17 -static -s

MV = mpc-vst-plugins
PLUGIN_SRC = plugin/engine.c plugin/plugin_tables.c $(CORE) $(MV)/wrapper/vst2_wrap.c
PLUGIN_DEPS = $(PLUGIN_SRC) $(DEPS) plugin/plugin_tables.h plugin/build/params.h
PLUGIN_FLAGS = -std=gnu11 -Icore -Iplugin -Iplugin/build -I$(MV)/wrapper
PLUGIN_DIR = plugin/build/plugin/ygn - VST - Torpedo Control
GEN_VST = SHADOW_ART=$(CURDIR)/plugin/html_art_local uv run --with pillow python $(MV)/tools/gen_vst.py plugin/vst.json

.PHONY: host tsan arm plugin-host plugin clean
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

plugin/build/params.h: plugin/vst.json plugin/params.json plugin/layout.conf
	$(GEN_VST) --params-h

build/plugin-host: plugin/test_host.c $(PLUGIN_DEPS)
	@mkdir -p $(@D)
	$(CC) -O1 -Wall -Wextra -Wno-unused-parameter $(PLUGIN_FLAGS) $(SAN) -o $@ plugin/test_host.c $(PLUGIN_SRC) -lpthread -lm

plugin-host: build/plugin-host

plugin/build/torpedo_control.so: $(PLUGIN_DEPS)
	$(ZIG) cc -target arm-linux-gnueabihf.2.31 -mcpu=cortex_a17 -O2 -Wall -Wextra -Wno-unused-parameter -fPIC -shared \
		-fvisibility=hidden -s $(PLUGIN_FLAGS) -o $@ $(PLUGIN_SRC) -lpthread -lm -Wl,--no-undefined
	@echo "highest glibc: $$(readelf -V $@ | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1) (MPC OS 2.x has 2.32)"
	@echo "exported: $$(readelf --dyn-syms -W $@ | grep -E ' GLOBAL .* [0-9]+ [A-Za-z]' | grep -v UND | awk '{print $$8}')"

plugin: plugin/build/torpedo_control.so plugin/images/*.svg
	$(GEN_VST)
	rm -rf plugin/build/plugin && mkdir -p plugin/build/plugin
	cp -r plugin/build/skin/* plugin/build/plugin/
	cp plugin/build/torpedo_control.so "$(PLUGIN_DIR)/"
	@# the plugin-list entry with the Synths folder left as %payload-path%: mpc-store.sh sync (and MockbaMod's
	@# vstscanner) register a folder from it, as for upstream's release zips (tools/release.py)
	sed 's|file="/sdcard/Synths/|file="%payload-path%/|' plugin/build/pluginlist-entry.xml > "$(PLUGIN_DIR)/plugin-meta.xml"

clean:
	rm -rf build plugin/build
