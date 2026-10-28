"""ตัวแทน map_saver_cli สำหรับทดสอบ: fake_map_saver.py <โหมด> -f <ไฟล์ไม่รวมนามสกุล>
โหมด: ok | fail | slow | empty | bad_yaml | no_files | partial"""
import sys
import time

import os
mode = sys.argv[1]
prefix = sys.argv[sys.argv.index('-f') + 1]
if mode == 'flaky_once':                       # ครั้งแรกล้ม ครั้งที่สองสำเร็จ (นับจากไฟล์ข้างนอกโฟลเดอร์แผนที่)
    counter = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(prefix))), 'attempts.txt')
    n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
    open(counter, 'w').write(str(n))
    mode = 'fail' if n == 1 else 'ok'
if mode == 'ok_args':                          # บันทึกอาร์กิวเมนต์ที่ได้รับ ไว้ตรวจ
    root = os.path.dirname(os.path.dirname(os.path.dirname(prefix)))
    open(os.path.join(root, 'args.txt'), 'w').write(' '.join(sys.argv[1:]))
    mode = 'ok'
name = prefix.rsplit('/', 1)[-1]
if mode == 'fail':
    print('[ERROR] Failed to spin map subscription', file=sys.stderr)
    sys.exit(1)
if mode == 'slow':
    time.sleep(30)
if mode == 'no_files':
    sys.exit(0)
open(prefix + '.pgm', 'wb').write(b'' if mode == 'empty' else b'P5\n2 2\n255\n\x00\xfe\xcd\x00')
if mode == 'partial':
    sys.exit(2)
good = f'image: {name}.pgm\nmode: trinary\nresolution: 0.05\norigin: [0, 0, 0]\nnegate: 0\n'
text = 'not: [valid' if mode == 'bad_yaml' else good
open(prefix + '.yaml', 'w').write(text)
