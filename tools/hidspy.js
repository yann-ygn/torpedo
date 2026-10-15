// Log Torpedo Remote's HID traffic: hidapi uses overlapped ReadFile/WriteFile with
// 65-byte buffers (report ID 0 + 64 bytes) and DeviceIoControl for feature reports.
const kb = Process.getModuleByName('KERNELBASE.dll');
const REPORT = 65;
const pendingReads = new Map();  // OVERLAPPED* -> buffer

function hex(ptr, n) {
  return Array.from(new Uint8Array(ptr.readByteArray(n)))
    .map(b => b.toString(16).padStart(2, '0')).join(' ');
}

Interceptor.attach(kb.getExportByName('WriteFile'), {
  onEnter(args) {
    if (args[2].toInt32() === REPORT)
      send({dir: 'out', h: args[0].toString(), data: hex(args[1], REPORT)});
  }
});

Interceptor.attach(kb.getExportByName('ReadFile'), {
  onEnter(args) {
    this.ok = args[2].toInt32() === REPORT;
    if (!this.ok) return;
    this.buf = args[1]; this.nread = args[3]; this.ol = args[4]; this.h = args[0];
    if (!this.ol.isNull()) pendingReads.set(this.ol.toString(), {buf: this.buf, h: this.h});
  },
  onLeave(ret) {
    if (this.ok && ret.toInt32() !== 0) {  // completed synchronously
      pendingReads.delete(this.ol.toString());
      const n = this.nread.isNull() ? REPORT : this.nread.readU32();
      if (n) send({dir: 'in', h: this.h.toString(), data: hex(this.buf, n)});
    }
  }
});

Interceptor.attach(kb.getExportByName('GetOverlappedResult'), {
  onEnter(args) { this.ol = args[1].toString(); this.n = args[2]; },
  onLeave(ret) {
    const r = pendingReads.get(this.ol);
    if (!r || ret.toInt32() === 0) return;
    pendingReads.delete(this.ol);
    const n = this.n.readU32();
    if (n) send({dir: 'in', h: r.h.toString(), data: hex(r.buf, n)});
  }
});

Interceptor.attach(kb.getExportByName('DeviceIoControl'), {
  onEnter(args) {
    const code = args[1].toUInt32();
    if ((code >>> 16) !== 0x0B) return;  // FILE_DEVICE_KEYBOARD: HID class IOCTLs
    this.code = code; this.out = args[4]; this.outLen = args[5].toInt32(); this.ret = args[6];
    const inLen = args[3].toInt32();
    send({dir: 'ioctl', code: code.toString(16), data: inLen ? hex(args[2], inLen) : ''});
  },
  onLeave(ret) {
    if (this.code && this.outLen && ret.toInt32() !== 0)
      send({dir: 'ioctl-out', code: this.code.toString(16),
            data: hex(this.out, Math.min(this.outLen, 128))});
  }
});

send({dir: 'info', data: 'hooks installed'});
