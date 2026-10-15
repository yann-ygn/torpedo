import time, hid
NAMES = """SIMUAMP SIMUAMP_MODELE SIMUAMP_GAIN SIMUAMP_CONTOUR SIMUAMP_DEPTH SIMUAMP_CHARACTER SIMUHP
SIMUHP_PREVIEW SIMUHP_SPEAKER SIMUHP_WAVE0 SIMUHP_WAVE1 SIMUHP_FOLDER_IR_0 SIMUHP_FOLDER_IR_1 SIMUHP_MIC
SIMUHP_DISTANCE SIMUHP_CENTER SIMUHP_POSITION EQ EQ_MODE EQ_L_GAIN0 EQ_L_GAIN1 EQ_L_GAIN2 EQ_L_GAIN3
EQ_L_GAIN4 PRESET_LEVEL REVERB REVERB_ROOM REVERB_L_DRYWET ARCADE_GUITARBASS ARCADE_POWERAMP
ARCADE_DISTANCE ARCADE_TONE ARCADE_CONTOUR ARCADE_LEVEL PRESET_MODE SIMUHP_LEVEL SIMUHP_PHASE SIMUHP_MUTE
SIMUHP_MIC_2 SIMUHP_DISTANCE_2 SIMUHP_CENTER_2 SIMUHP_POSITION_2 SIMUHP_LEVEL_2 SIMUHP_PHASE_2
SIMUHP_MUTE_2 EQ_L_FREQ_LOW EQ_L_FREQ_F0 EQ_L_FREQ_F1 EQ_L_FREQ_F2 EQ_L_FREQ_F3 EQ_L_FREQ_F4""".split()
d = hid.device(); d.open(0x0483, 0xA334); d.set_nonblocking(1)
def send(*b): d.write([0] + list(b) + [0] * (64 - len(b)))
def collect(t=0.6):
    out, end = [], time.time() + t
    while time.time() < end:
        p = d.read(65)
        if p: out.append(bytes(p))
        else: time.sleep(0.001)
    return out
collect(0.2)
send(0x04, 0x01)
for p in collect():
    if p[0] == 0x0b: print("info:", p[:20].hex(" "), "serial", p[7:15].decode())
send(0x04, 0x0a)
params, end = {}, time.time() + 3
while len(params) < len(NAMES) and time.time() < end:
    params.update((p[1], p[2]) for p in collect(0.1) if p[0] == 0x01)
print(len(params), "params")
for i, n in enumerate(NAMES):
    print(f"  {i:02x} {n:<20} {params.get(i)}")
d.close()
