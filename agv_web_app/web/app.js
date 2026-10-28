// AMR Control — Home (Dashboard) + เมนูโหมด ; UI 2 ภาษา (EN เริ่มต้น / TH) ผ่าน i18n.js
// รับสถานะจาก WebSocket /ws ; คำสั่ง (login / เปลี่ยนโหมด) ส่งผ่าน REST POST เท่านั้น
// ข้อความที่ผู้ใช้เห็นทั้งหมดมาจาก t() / tm() (ห้ามเขียนประโยคตายตัวในไฟล์นี้) และใช้ textContent เท่านั้น
// (ไม่ใช้ innerHTML) เพราะข้อมูลมาจากภายนอก ; ตรรกะ "พร้อมไหม" และ "โหมดจริง" ตัดสินที่ backend
// ไฟล์นี้เป็นต้นแบบ: เนื้อหาแยกอยู่ใน js/*.js แล้วเซิร์ฟเวอร์ประกอบกลับเป็นไฟล์เดียวตอนส่ง (agv_web_app/web_assets.py)
// ลำดับของจุดแทรกด้านล่างคือลำดับการทำงานจริง (closure เดียวกัน) — เรียงใหม่ = เปลี่ยนพฤติกรรม
(function () {
  'use strict';

  // @include js/00_core.js
  // @include js/10_home.js
  // @include js/11_auth.js
  // @include js/12_mode.js
  // @include js/13_charts.js
  // @include js/14_stats.js
  // @include js/20_map_view.js
  // @include js/21_slam.js
  // @include js/22_display_laser.js
  // @include js/23_drive.js
  // @include js/24_save_map.js
  // @include js/30_map_library.js
  // @include js/31_nav_params.js
  // @include js/32_nav_pose.js
  // @include js/33_nav_goal.js
  // @include js/34_nav_start.js
  // @include js/36_map_interaction.js
  // @include js/40_waypoint.js
  // @include js/41_route_run.js
  // @include js/42_line_run.js
  // @include js/90_app_start.js
})();
