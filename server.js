// 启动器：为 server.py 找到可用的 Python 解释器并转发参数
// 依次尝试：环境变量 PYTHON → 系统 python → Windows py 启动器
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const serverPy = path.join(__dirname, 'server.py');

const candidates = [
  process.env.PYTHON,
  'python',
  'py',
].filter(Boolean);

function works(cmd) {
  try {
    if (cmd.includes('\\') || cmd.includes('/')) {
      if (!fs.existsSync(cmd)) return false;
    }
    const r = spawnSync(cmd, ['-c', 'import sys; print(sys.version_info[0])'], {
      encoding: 'utf8', timeout: 15000, windowsHide: true,
    });
    return r.status === 0 && (r.stdout || '').trim().startsWith('3');
  } catch (e) {
    return false;
  }
}

let python = null;
for (const c of candidates) {
  if (works(c)) { python = c; break; }
}

if (!python) {
  console.error('[dlsite-compare] 找不到可用的 Python 3 解释器。');
  console.error('请安装 Python 3，或设置环境变量 PYTHON 指向 python.exe。');
  process.exit(1);
}

console.log('[dlsite-compare] 使用 Python: ' + python);
const child = spawn(python, [serverPy, ...process.argv.slice(2)], {
  stdio: 'inherit',
  windowsHide: true,
});
child.on('exit', (code) => process.exit(code == null ? 0 : code));
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
