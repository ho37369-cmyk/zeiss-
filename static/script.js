// Zeiss CZI Automatic Cell Counter — Frontend
let uploadedFiles = []; // { filepath, filename, scenes, channels, previews, error? }
let channelSelections = {}; // { filepath: { live_channel, dead_channel } }

// ========== Drag & Drop ==========

// ---- 1. Document-level prevention: stops Edge from hijacking the drop ----
// Use capture phase so we intercept BEFORE Edge's built-in handler
document.addEventListener('dragover', function(e) {
    e.preventDefault();
    e.stopPropagation();
}, true);
document.addEventListener('drop', function(e) {
    e.preventDefault();
    e.stopPropagation();
}, true);
// Also keep bubbling-phase listeners as a belt-and-suspenders fallback
document.addEventListener('dragover', function(e) { e.preventDefault(); e.stopPropagation(); });
document.addEventListener('drop', function(e) { e.preventDefault(); e.stopPropagation(); });
// Block dragenter too — some Edge versions use it to trigger download prompts
document.addEventListener('dragenter', function(e) { e.preventDefault(); e.stopPropagation(); }, true);

// ---- 2. Drop zone ----
const dropZone = document.getElementById('dropZone');
if (dropZone) {
    dropZone.addEventListener('dragenter', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });
    dropZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('dragover');

        const files = Array.from(e.dataTransfer.files);
        const items = Array.from(e.dataTransfer.items);

        // Check if user dropped text (path text)
        for (const item of items) {
            if (item.kind === 'string' && item.type === 'text/plain') {
                item.getAsString((text) => {
                    const textPaths = text.split('\n').map(s => s.trim()).filter(s => s);
                    if (textPaths.length > 0) addPaths(textPaths);
                });
                return;
            }
        }

        // Filter only .czi files
        const cziFiles = files.filter(f => f.name && f.name.toLowerCase().endsWith('.czi'));
        if (cziFiles.length === 0) {
            showPathInput();
            return;
        }

        // Upload via FormData
        const formData = new FormData();
        for (const f of cziFiles) {
            formData.append('files', f, f.name);
        }

        // Show uploading indicator
        const fileList = document.getElementById('fileList');
        fileList.classList.remove('hidden');
        fileList.innerHTML = '<div style="text-align:center;padding:20px;color:#666;">正在上传 ' + cziFiles.length + ' 个文件...</div>';

        try {
            const resp = await fetch('/upload_file', {
                method: 'POST',
                body: formData,
            });
            const data = await resp.json();
            if (data.error) {
                alert(data.error);
                fileList.classList.add('hidden');
                return;
            }
            renderFileList(data.files);
        } catch (e) {
            alert('上传失败: ' + e.message);
            fileList.classList.add('hidden');
        }
    });
}
// ========== Path Input ==========
function showPathInput() {
    const fileList = document.getElementById('fileList');
    fileList.classList.remove('hidden');
    fileList.innerHTML = `
        <div style="background:white;border-radius:8px;padding:16px;">
            <label style="font-weight:600;font-size:14px;display:block;margin-bottom:6px;">
                粘贴文件或文件夹路径：
            </label>
            <textarea id="pathInput" rows="4" style="width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;font-size:13px;font-family:monospace;"
                placeholder="一行一个路径，支持文件夹（自动递归扫描 .czi 文件）：&#10;C:\Users\MyData\experiment1.czi&#10;D:\cell_images\"></textarea>
            <div style="margin-top:8px;display:flex;gap:8px;">
                <button class="btn btn-primary" onclick="submitPaths()">扫描文件</button>
                <button class="btn btn-secondary" onclick="hidePathInput()">取消</button>
            </div>
        </div>
    `;
}

function hidePathInput() {
    const fileList = document.getElementById('fileList');
    fileList.classList.add('hidden');
}

async function submitPaths() {
    const input = document.getElementById('pathInput');
    const paths = input.value.split('\n').map(s => s.trim()).filter(s => s);
    if (paths.length === 0) return;
    addPaths(paths);
}

async function addPaths(paths) {
    try {
        const resp = await fetch('/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths }),
        });
        const data = await resp.json();
        if (data.error) {
            alert(data.error);
            return;
        }
        renderFileList(data.files);
    } catch (e) {
        alert('连接失败: ' + e.message);
    }
}

// ========== File List & Channel Selection ==========
function renderFileList(files) {
    uploadedFiles = files;
    const fileList = document.getElementById('fileList');
    fileList.classList.remove('hidden');

    let html = '<div style="margin-bottom:10px;font-size:14px;font-weight:600;">已发现 <strong>' +
               files.filter(f => !f.error).length + '</strong> 个 CZI 文件：</div>';

    for (const f of files) {
        if (f.error) {
            html += '<div class="file-item"><span class="file-icon">&#9888;</span>';
            html += '<span class="file-name">' + f.filename + '</span>';
            html += '<span class="file-status" style="color:#e53935;">错误: ' + f.error + '</span></div>';
            continue;
        }

        html += '<div class="file-item">';
        html += '<span class="file-icon">&#128196;</span>';
        html += '<span class="file-name">' + f.filename + '</span>';
        if (f.scenes.length > 1) {
            html += '<span class="file-status">(' + f.scenes.length + ' scenes)</span>';
        }
        html += '</div>';

        // Channel selector for this file
        html += '<div class="channel-section" id="ch-' + escapeId(f.filepath) + '">';
        html += '<h3>' + f.filename + ' — 通道选择</h3>';

        // Scene previews
        for (const sName in f.previews) {
            const channels = f.previews[sName];
            html += '<div style="margin-bottom:10px;">';
            if (f.scenes.length > 1) {
                html += '<div style="font-size:12px;color:#666;margin-bottom:6px;"><strong>' + sName + '</strong></div>';
            }
            for (const ch of channels) {
                html += '<div class="channel-card">';
                html += '<img src="data:image/jpeg;base64,' + ch.b64 + '" alt="' + ch.name + '">';
                html += '<div class="channel-card-info">';
                html += '<div class="name">' + ch.name + '</div>';
                const chInfo = f.channels.find(c => c.index === ch.index) || {};
                html += '<div class="type">' + (chInfo.channel_type || 'unknown') + (chInfo.dye ? ' &middot; ' + chInfo.dye : '') + '</div>';
                html += '</div>';
                html += '</div>';
            }
            if (f.scenes.length > 1) break; // Only show first scene preview
        }

        // Selectors
        html += '<div style="display:flex;gap:12px;margin-top:10px;flex-wrap:wrap;">';
        html += '<label style="font-size:13px;">活细胞通道: ';
        html += '<select class="channel-select" onchange="updateSelection(\'' + escapeAttr(f.filepath) + '\', \'live\', this.value)">';
        for (const ch of f.channels) {
            const selected = ch.channel_type === 'brightfield' || ch.index === 0 ? 'selected' : '';
            html += '<option value="' + ch.index + '" ' + selected + '>' + ch.name + '</option>';
        }
        html += '</select></label>';

        html += '<label style="font-size:13px;">死细胞通道 (可选): ';
        html += '<select class="channel-select" onchange="updateSelection(\'' + escapeAttr(f.filepath) + '\', \'dead\', this.value)">';
        html += '<option value="">— 不做死活区分 —</option>';
        for (const ch of f.channels) {
            // Suggest PI/DAPI/Tritc as default dead channel
            const isDead = ch.dye === 'tritc' || ch.dye === 'dapi' || ch.dye === 'cy5';
            const selected = isDead ? 'selected' : '';
            html += '<option value="' + ch.index + '" ' + selected + '>' + ch.name + '</option>';
        }
        html += '</select></label>';
        html += '</div>';
        html += '</div>';
    }

    fileList.innerHTML = html;

    // Initialize selections
    for (const f of files) {
        if (f.error) continue;
        const liveIdx = f.channels.find(c => c.channel_type === 'brightfield' || c.index === 0);
        const deadIdx = f.channels.find(c => c.dye === 'tritc' || c.dye === 'dapi' || c.dye === 'cy5');
        channelSelections[f.filepath] = {
            filepath: f.filepath,
            live_channel: liveIdx ? liveIdx.index : 0,
            dead_channel: deadIdx ? deadIdx.index : null,
            channels_info: f.channels,
        };
    }

    // Show Process button
    document.getElementById('processArea').classList.remove('hidden');
}

function updateSelection(filepath, type, value) {
    const sel = channelSelections[filepath] || { filepath, channels_info: [] };
    if (type === 'live') {
        sel.live_channel = parseInt(value);
    } else {
        sel.dead_channel = value ? parseInt(value) : null;
    }
    channelSelections[filepath] = sel;
}

function escapeId(str) {
    return str.replace(/[^a-zA-Z0-9]/g, '_');
}

function escapeAttr(str) {
    return str.replace(/'/g, "\\'");
}

// ========== Processing ==========
async function startProcess() {
    const selections = Object.values(channelSelections);
    if (selections.length === 0) {
        alert('请先添加文件');
        return;
    }

    // Show progress
    document.getElementById('step-upload').classList.add('hidden');
    document.getElementById('step-progress').classList.remove('hidden');
    document.getElementById('step-results').classList.add('hidden');

    try {
        const resp = await fetch('/process', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ selections }),
        });
        const data = await resp.json();
        if (data.error) {
            alert(data.error);
            return;
        }
        listenProgress(data.task_id);
    } catch (e) {
        alert('启动失败: ' + e.message);
    }
}

function listenProgress(taskId) {
    const evtSource = new EventSource('/progress/' + taskId);
    evtSource.onmessage = function(event) {
        const data = JSON.parse(event.data);
        const fill = document.getElementById('progressFill');
        const text = document.getElementById('progressText');

        if (data.status === 'complete') {
            fill.style.width = '100%';
            text.textContent = '处理完成！';
            evtSource.close();
            showResults(taskId, data);
        } else if (data.status === 'error') {
            text.textContent = '错误: ' + data.msg;
            evtSource.close();
        } else {
            fill.style.width = data.progress + '%';
            text.textContent = '处理中... ' + data.done + ' / ' + data.total;
        }
    };
    evtSource.onerror = function() {
        document.getElementById('progressText').textContent = '连接断开，请刷新页面查看结果';
        evtSource.close();
    };
}

function showResults(taskId, data) {
    const resultsDiv = document.getElementById('step-results');
    resultsDiv.classList.remove('hidden');

    let html = '<div class="actions">';
    html += '<a href="/download_zip/' + taskId + '" class="btn btn-primary">下载标注图 (ZIP)</a>';
    if (data.excel_file) {
        html += '<a href="/download/' + taskId + '/' + data.excel_file + '" class="btn btn-primary">下载 Excel 报告</a>';
    }
    html += '<a href="/" class="btn btn-secondary">新一批处理</a>';
    html += '</div>';

    if (data.errors && data.errors.length > 0) {
        html += '<div class="error-box"><strong>处理警告：</strong><br>';
        for (const e of data.errors) {
            html += e + '<br>';
        }
        html += '</div>';
    }

    if (data.results && data.results.length > 0) {
        html += '<div class="results-gallery">';
        for (const r of data.results) {
            const sceneLabel = r.scene !== 'Scene0' ? ' (' + r.scene + ')' : '';
            html += '<div class="result-card">';
            html += '<a href="/download/' + taskId + '/' + r.annotated_file + '" target="_blank">';
            html += '<img src="/download/' + taskId + '/' + r.annotated_file + '">';
            html += '</a>';
            html += '<div class="result-card-body">';
            html += '<div class="filename">' + r.filename + sceneLabel + '</div>';
            html += '<div class="stats">总细胞: <strong>' + r.total + '</strong><br>';
            html += '<span class="live">活细胞: ' + r.live + '</span><br>';
            if (r.has_dead) {
                html += '<span class="dead">死细胞: ' + r.dead + '</span><br>';
            }
            html += '存活率: ' + r.viability.toFixed(1) + '%';
            html += '</div></div></div>';
        }
        html += '</div>';
    } else {
        html += '<p style="text-align:center;color:#999;margin:40px 0;">无有效结果</p>';
    }

    resultsDiv.innerHTML = html;
}

// ========== File Input Button ==========
async function handleFileInput(files) {
    if (!files || files.length === 0) return;

    const fileList = document.getElementById('fileList');
    fileList.classList.remove('hidden');
    fileList.innerHTML = '<div style="text-align:center;padding:20px;color:#666;">正在上传 ' + files.length + ' 个文件...</div>';

    const formData = new FormData();
    for (const f of files) {
        formData.append('files', f, f.name);
    }

    try {
        const resp = await fetch('/upload_file', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (data.error) {
            alert(data.error);
            fileList.classList.add('hidden');
            return;
        }
        renderFileList(data.files);
    } catch (e) {
        alert('上传失败: ' + e.message);
        fileList.classList.add('hidden');
    }

    document.getElementById('fileInput').value = '';
}

// Allow clicking drop zone to trigger path input
document.addEventListener('DOMContentLoaded', function() {
    if (dropZone) {
        dropZone.addEventListener('click', function(e) {
            if (!e.target.closest('button') && !e.target.closest('label')) {
                showPathInput();
            }
        });
    }

    // File input button wiring
    const fileInput = document.getElementById('fileInput');
    const btnSelectFiles = document.getElementById('btnSelectFiles');
    const btnPathInput = document.getElementById('btnPathInput');

    if (btnSelectFiles && fileInput) {
        btnSelectFiles.addEventListener('click', function(e) {
            e.stopPropagation();
            fileInput.click();
        });
        fileInput.addEventListener('change', function() {
            handleFileInput(fileInput.files);
        });
    }

    if (btnPathInput) {
        btnPathInput.addEventListener('click', function(e) {
            e.stopPropagation();
            showPathInput();
        });
    }
});
