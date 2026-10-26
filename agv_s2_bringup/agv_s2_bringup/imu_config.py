# เครื่องมือตั้งค่า WitMotion HWT901B (TTL) ผ่าน serial — ใช้ครั้งเดียวก่อนรัน driver
# อ้างอิง: HWT901B-TTL Datasheet v20-0707 หัวข้อ 5.2 (Config Commands)
#
# รูปแบบคำสั่ง: 0xFF 0xAA <register> <DataL> <DataH>
# รูปแบบ packet ขาออกจาก IMU: 0x55 <type> <8 byte data> <checksum>  (รวม 11 byte)
#
# ตัวอย่าง:
#   ros2 run agv_s2_bringup imu_config --check              # อ่านอย่างเดียว ดูค่าปัจจุบัน
#   ros2 run agv_s2_bringup imu_config --dry-run            # แสดงคำสั่งที่จะส่ง ไม่ส่งจริง
#   ros2 run agv_s2_bringup imu_config                      # ตั้งค่าจริง (6 แกน, 100 Hz, 115200)
#   ros2 run agv_s2_bringup imu_config --factory-reset      # คืนค่าโรงงาน
import argparse
import collections
import struct
import sys
import time

import serial

# ── ตาราง register / ค่า ตาม datasheet ──────────────────────────────────────
REG_SAVE = 0x00        # 0 = save, 1 = factory default
REG_RSW = 0x02         # packet ที่ให้ส่งออก (bitmask)
REG_RATE = 0x03        # output rate
REG_BAUD = 0x04        # baud rate
REG_ALGORITHM = 0x24   # 0 = 9 แกน, 1 = 6 แกน

# unlock: firmware รุ่นใหม่ต้องส่งก่อนคำสั่งเขียน (datasheet ฉบับนี้ไม่ได้ระบุ)
# firmware รุ่นเก่าจะเพิกเฉยคำสั่งนี้ จึงส่งไว้ก่อนเสมอได้อย่างปลอดภัย
UNLOCK = bytes([0xFF, 0xAA, 0x69, 0x88, 0xB5])

RATE_CODE = {0.2: 0x01, 0.5: 0x02, 1: 0x03, 2: 0x04, 5: 0x05, 10: 0x06,
             20: 0x07, 50: 0x08, 100: 0x09, 125: 0x0A, 200: 0x0B}
BAUD_CODE = {4800: 0x01, 9600: 0x02, 19200: 0x03, 38400: 0x04, 57600: 0x05,
             115200: 0x06, 230400: 0x07, 460800: 0x08, 921600: 0x09}
# ลำดับการลองหา baud: ค่าที่น่าจะเป็นที่สุดก่อน
BAUD_SCAN = [9600, 115200, 230400, 460800, 921600, 57600, 38400, 19200, 4800]

# RSW: bit n ของ RSWL = packet 0x50+n  → 0x51 accel, 0x52 gyro, 0x53 angle
PACKETS = (0x51, 0x52, 0x53)
PACKET_NAME = {0x50: 'time', 0x51: 'accel', 0x52: 'gyro', 0x53: 'angle',
               0x54: 'mag', 0x56: 'pressure', 0x59: 'quaternion'}
FRAME_LEN = 11


def cmd(register, value):
    """สร้างคำสั่ง 5 byte: FF AA reg DataL DataH (value เป็น 16 บิต little-endian)."""
    return bytes([0xFF, 0xAA, register, value & 0xFF, (value >> 8) & 0xFF])


def rsw_mask(packets):
    mask = 0
    for p in packets:
        mask |= 1 << (p - 0x50)
    return mask


def hexs(data):
    return ' '.join(f'{b:02X}' for b in data)


def parse_frames(buf):
    """หา frame ที่ขึ้นต้นด้วย 0x55 และ checksum ถูก (ผลรวม 10 byte แรก & 0xFF)."""
    frames, i = [], 0
    while i + FRAME_LEN <= len(buf):
        if buf[i] == 0x55 and (sum(buf[i:i + 10]) & 0xFF) == buf[i + 10]:
            frames.append(bytes(buf[i:i + FRAME_LEN]))
            i += FRAME_LEN
        else:
            i += 1   # ไม่ใช่ frame → เลื่อนไป 1 byte แล้วหาใหม่ (resync)
    return frames


def sniff(port, baud, seconds=2.0):
    """อ่านอย่างเดียว คืนค่า (จำนวน byte, list ของ frame ที่ถูกต้อง)."""
    with serial.Serial(port, baud, timeout=0.2) as s:
        s.reset_input_buffer()
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < seconds:
            buf += s.read(4096)
    return len(buf), parse_frames(buf)


def detect_baud(port):
    print(f'[detect] กำลังหา baud ปัจจุบันของ {port} ...')
    for baud in BAUD_SCAN:
        _, frames = sniff(port, baud, seconds=1.0)
        print(f'  {baud:>6}: valid frames {len(frames)}')
        if len(frames) >= 3:
            return baud
    return None


def report(port, baud, seconds=2.0):
    nbytes, frames = sniff(port, baud, seconds)
    count = collections.Counter(f[1] for f in frames)
    print(f'[check] {port} @ {baud} baud: {nbytes} bytes / {seconds:.0f} s, '
          f'valid frames {len(frames)}')
    for ptype, n in sorted(count.items()):
        name = PACKET_NAME.get(ptype, '?')
        print(f'  0x{ptype:02X} {name:<10} ~{n / seconds:6.1f} Hz')
    last = {f[1]: f for f in frames}
    if 0x51 in last:
        ax, ay, az, t = struct.unpack('<hhhh', last[0x51][2:10])
        print(f'  accel [g]    : {ax / 32768 * 16:+.3f} {ay / 32768 * 16:+.3f} '
              f'{az / 32768 * 16:+.3f}   temp {t / 100:.1f} C')
    if 0x52 in last:
        wx, wy, wz, _ = struct.unpack('<hhhh', last[0x52][2:10])
        print(f'  gyro [deg/s] : {wx / 32768 * 2000:+.3f} {wy / 32768 * 2000:+.3f} '
              f'{wz / 32768 * 2000:+.3f}')
    if 0x53 in last:
        r, p, y, ver = struct.unpack('<hhhH', last[0x53][2:10])
        print(f'  angle [deg]  : roll {r / 32768 * 180:+.2f} pitch {p / 32768 * 180:+.2f} '
              f'yaw {y / 32768 * 180:+.2f}   firmware version {ver}')
    # baud ใช้ไปกี่ % (1 byte บน serial = 10 bit: start + 8 data + stop)
    load = nbytes / seconds * 10 / baud * 100
    print(f'  bandwidth    : ~{load:.0f}% ของ {baud} baud')
    return count


def build_plan(args):
    """คืน list ของ (คำอธิบาย, bytes) ที่จะส่งที่ baud เดิม."""
    if args.factory_reset:
        return [('unlock', UNLOCK),
                ('คืนค่าโรงงาน (factory default)', cmd(REG_SAVE, 1))]
    plan = [('unlock', UNLOCK)]
    if args.six_axis:
        plan.append(('algorithm = 6 แกน (ปิด magnetometer)', cmd(REG_ALGORITHM, 1)))
    mask = rsw_mask(PACKETS)
    names = ', '.join(f'0x{p:02X}' for p in PACKETS)
    plan += [
        (f'return content = {names}', cmd(REG_RSW, mask)),
        (f'rate = {args.rate:g} Hz', cmd(REG_RATE, RATE_CODE[args.rate])),
        ('save', cmd(REG_SAVE, 0)),
    ]
    return plan


def send(ser, label, data, dry_run):
    print(f'  {"[dry-run] " if dry_run else ""}{hexs(data):<16}  {label}')
    if not dry_run:
        ser.write(data)
        ser.flush()
        time.sleep(0.2)   # รอ IMU ประมวลผลคำสั่ง


def main():
    ap = argparse.ArgumentParser(description='ตั้งค่า WitMotion HWT901B (TTL)')
    ap.add_argument('--port', default='/dev/imu')
    ap.add_argument('--baud', type=int, default=None,
                    help='baud ปัจจุบันของ IMU (ไม่ระบุ = หาอัตโนมัติ)')
    ap.add_argument('--new-baud', type=int, default=115200, choices=sorted(BAUD_CODE))
    ap.add_argument('--rate', type=float, default=100, choices=sorted(RATE_CODE))
    ap.add_argument('--nine-axis', dest='six_axis', action='store_false',
                    help='ใช้ 9 แกน (เปิด magnetometer) — ไม่แนะนำในโรงงาน')
    ap.add_argument('--check', action='store_true', help='อ่านอย่างเดียว แล้วจบ')
    ap.add_argument('--dry-run', action='store_true', help='แสดงคำสั่ง ไม่ส่งจริง')
    ap.add_argument('--factory-reset', action='store_true')
    args, _ = ap.parse_known_args()   # ignore --ros-args ที่ ros2 run ส่งมา

    # rate * 33 byte (3 packet) * 10 bit ต้องไม่เกิน ~80% ของ baud
    need = args.rate * FRAME_LEN * len(PACKETS) * 10
    if not args.factory_reset and need > 0.8 * args.new_baud:
        sys.exit(f'[error] {args.rate:g} Hz ต้องการ ~{need:.0f} bit/s '
                 f'เกิน 80% ของ {args.new_baud} baud → เพิ่ม --new-baud')

    baud = args.baud or detect_baud(args.port)
    if baud is None:
        sys.exit('[error] ไม่พบข้อมูลจาก IMU ทุก baud → ตรวจสายไฟ / TX-RX ไขว้')
    print()
    report(args.port, baud)
    if args.check:
        return

    print(f'\n[config] ส่งคำสั่งที่ {baud} baud:')
    with serial.Serial(args.port, baud, timeout=0.2) as ser:
        for label, data in build_plan(args):
            send(ser, label, data, args.dry_run)

        if args.factory_reset:
            if not args.dry_run:
                print('\nคืนค่าโรงงานแล้ว → ถอดไฟ IMU เสียบใหม่ แล้วรัน --check '
                      '(ค่าโรงงาน = 9600 baud, 10 Hz)')
            return

        # เปลี่ยน baud เป็นคำสั่ง "สุดท้าย" เพราะ IMU จะสลับ baud ทันทีหลังได้รับ
        if args.new_baud != baud:
            send(ser, f'baud = {args.new_baud}', cmd(REG_BAUD, BAUD_CODE[args.new_baud]),
                 args.dry_run)

    if args.new_baud != baud:
        # เปิด port ใหม่ที่ baud ใหม่ แล้ว unlock + save อีกรอบเพื่อบันทึก baud
        print(f'\n[config] เปิด port ใหม่ที่ {args.new_baud} baud แล้ว save:')
        if not args.dry_run:
            time.sleep(0.5)
        with serial.Serial(args.port, args.new_baud, timeout=0.2) as ser:
            send(ser, 'unlock', UNLOCK, args.dry_run)
            send(ser, 'save', cmd(REG_SAVE, 0), args.dry_run)

    if args.dry_run:
        print('\n[dry-run] ไม่ได้ส่งคำสั่งใด ๆ ไปที่ IMU')
        return

    print('\n[verify] อ่านค่าหลังตั้งค่า (rate อาจยังไม่เปลี่ยนจนกว่าจะถอดไฟ):')
    found = detect_baud(args.port)
    if found is None:
        sys.exit('[error] หา IMU ไม่เจอหลังตั้งค่า → ถอดไฟเสียบใหม่ แล้วรัน --check')
    report(args.port, found)
    print('\nขั้นต่อไป: ถอดไฟ IMU เสียบใหม่ แล้วรัน\n'
          f'  ros2 run agv_s2_bringup imu_config --check --baud {args.new_baud}\n'
          f'ต้องเห็นเฉพาะ 0x51/0x52/0x53 ที่ ~{args.rate:g} Hz')


if __name__ == '__main__':
    main()
