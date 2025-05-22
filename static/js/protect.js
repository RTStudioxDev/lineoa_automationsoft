(function () {
  let alerted = false;
  function fbi() {
    if (!alerted) {
      alerted = true;
      document.body.innerHTML = `
        <div style="font-size:2em;text-align:center;margin-top:30vh;color:red;">
          Copyright © RTStudioXCode!<br>
          ห้ามเปิด Inspect/Console<br>
          <img src="https://media2.giphy.com/media/v1.Y2lkPTc5MGI3NjExOHJ6aWtkMmRtbjZpZGd1M3N6dG52aWdtYXZyM3B0MGw4cGU0bTFkYyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/tJeGZumxDB01q/giphy.gif"
     style="width:400px; height:400px; object-fit:cover; border-radius:50%; border:4px solid #fff; box-shadow:0 2px 16px rgba(0,0,0,0.10); background:#fff;">
        </div>
      `;
      setTimeout(() => location.reload(), 3000); // หรือจะ redirect ก็ได้
    }
  }
  // ตรวจ devtools (ขนาดจอ)
  setInterval(function () {
    let threshold = 160;
    if (
      window.outerWidth - window.innerWidth > threshold ||
      window.outerHeight - window.innerHeight > threshold
    ) {
      fbi();
    }
  }, 500);
  // ปิดคลิกขวา/คัดลอก/ลาก
  document.addEventListener('contextmenu', e => e.preventDefault());
  document.addEventListener('selectstart', e => e.preventDefault());
  document.addEventListener('dragstart', e => e.preventDefault());
  // ปิดคีย์ Ctrl+U, F12, Ctrl+Shift+I, Ctrl+Shift+C, Ctrl+S
  document.addEventListener('keydown', function (e) {
    if (
      e.keyCode == 123 // F12
      || (e.ctrlKey && e.shiftKey && e.keyCode == 73) // Ctrl+Shift+I
      || (e.ctrlKey && e.shiftKey && e.keyCode == 67) // Ctrl+Shift+C
      || (e.ctrlKey && e.keyCode == 85) // Ctrl+U
      || (e.ctrlKey && e.keyCode == 83) // Ctrl+S
    ) {
      fbi();
      e.preventDefault();
      return false;
    }
  });
})();
