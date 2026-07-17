import os, sys, json, uuid, threading, zipfile, io, time, base64, webbrowser
from datetime import datetime
from pathlib import Path
import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify, Response, send_file, stream_with_context, session

# --- PyInstaller bundled-exe path handling ---
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
    APP_DIR = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

sys.path.insert(0, str(BUNDLE_DIR))
from cell_counter import read_czi, classify_channels, count_cells, annotate_image, create_dead_mask, write_excel

app = Flask(__name__,
    template_folder=str(BUNDLE_DIR / 'templates'),
    static_folder=str(BUNDLE_DIR / 'static'))
app.secret_key = os.urandom(24)

OUTPUT_DIR = APP_DIR / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)

# In-memory task store
tasks = {}
tasks_lock = threading.Lock()


def new_task_id():
    return uuid.uuid4().hex[:12]


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_files():
    data = request.get_json(force=True)
    paths = data.get('paths', [])
    if not paths:
        return jsonify({'error': 'No file paths provided'}), 400

    # Collect all .czi files from paths (recursively if directories)
    czi_files = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            czi_files.extend(sorted(pp.rglob('*.czi')))
        elif pp.is_file() and pp.suffix.lower() == '.czi':
            czi_files.append(pp)

    if not czi_files:
        return jsonify({'error': 'No .czi files found in the provided paths'}), 400

    # Analyze each CZI file
    analyzed = []
    for cf in czi_files:
        try:
            data = read_czi(str(cf))
            channels = classify_channels(data['channel_metadata'], data['scenes'])
            previews = {}
            for s_name, s_data in data['scenes'].items():
                previews[s_name] = []
                for ch in s_data['channels']:
                    img_small = _thumbnail(ch['image'], max_w=120, max_h=120)
                    _, buf = cv2.imencode('.jpg', img_small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    b64 = base64.b64encode(buf).decode('utf-8')
                    previews[s_name].append({
                        'index': ch['index'],
                        'name': ch['name'],
                        'b64': b64,
                    })

            analyzed.append({
                'filepath': str(cf),
                'filename': cf.name,
                'scenes': list(data['scenes'].keys()),
                'channels': [
                    {'index': c.index, 'name': c.name, 'channel_type': c.channel_type, 'dye': c.dye}
                    for c in channels
                ],
                'previews': previews,
            })
        except Exception as e:
            analyzed.append({
                'filepath': str(cf),
                'filename': cf.name,
                'error': str(e),
            })

    return jsonify({'files': analyzed})


@app.route('/process', methods=['POST'])
def start_processing():
    data = request.get_json(force=True)
    selections = data.get('selections', [])
    if not selections:
        return jsonify({'error': 'No selections'}), 400

    task_id = new_task_id()
    with tasks_lock:
        tasks[task_id] = {
            'status': 'running',
            'progress': 0,
            'total': len(selections),
            'done': 0,
            'results': [],
            'errors': [],
        }

    def worker():
        with tasks_lock:
            t = tasks[task_id]
        output_dir = OUTPUT_DIR / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        annotated_dir = output_dir / 'annotated'
        annotated_dir.mkdir(exist_ok=True)

        for i, sel in enumerate(selections):
            filepath = sel['filepath']
            live_ch = sel.get('live_channel', 0)
            dead_ch = sel.get('dead_channel')
            filename = Path(filepath).name

            try:
                czi_data = read_czi(filepath)
                for s_name, s_data in czi_data['scenes'].items():
                    # Find live and dead channel images
                    live_img = None
                    dead_img = None
                    for ch in s_data['channels']:
                        if ch['index'] == live_ch:
                            live_img = ch['image']
                        if dead_ch is not None and ch['index'] == dead_ch:
                            dead_img = ch['image']

                    if live_img is None:
                        t['errors'].append(f'{filename} - {s_name}: Live channel not found')
                        continue

                    # Count cells
                    ch_info = None
                    for c in sel.get('channels_info', []):
                        if c['index'] == live_ch:
                            ch_info = c
                            break
                    ch_type = (ch_info or {}).get('channel_type', 'fluorescence')
                    total_result = count_cells(live_img, ch_type)

                    # Dead cell detection
                    dead_mask = None
                    if dead_ch is not None and dead_img is not None:
                        dead_result = count_cells(dead_img, 'fluorescence')
                        dead_mask = create_dead_mask(dead_result)

                    # Annotate
                    annotated = annotate_image(live_img, total_result, dead_mask)

                    # Determine counts
                    dead_count = 0
                    if dead_mask is not None and total_result['labels'] is not None:
                        unique = np.unique(total_result['labels'][dead_mask > 0])
                        dead_count = int((unique > 0).sum())
                    live_count = total_result['total'] - dead_count
                    viability = (live_count / total_result['total'] * 100) if total_result['total'] > 0 else 0

                    # Build cell details
                    cell_details = []
                    dead_labels = set()
                    if dead_mask is not None and total_result['labels'] is not None:
                        unique = np.unique(total_result['labels'][dead_mask > 0])
                        dead_labels = set(int(l) for l in unique if l > 0)
                    for prop in total_result['props']:
                        cell_details.append({
                            'label': prop['label'],
                            'area': prop['area'],
                            'circularity': prop['circularity'],
                            'mean_intensity': prop['mean_intensity'],
                            'is_dead': prop['label'] in dead_labels,
                        })

                    # Save annotated image
                    scene_suffix = f'_{s_name}' if s_name != 'Scene0' else ''
                    safe_fn = Path(filename).stem + scene_suffix + '.png'
                    out_path = annotated_dir / safe_fn
                    cv2.imwrite(str(out_path), annotated)

                    result_item = {
                        'filename': filename,
                        'scene': s_name,
                        'total': total_result['total'],
                        'live': live_count,
                        'dead': dead_count,
                        'viability': viability,
                        'has_dead': dead_ch is not None,
                        'annotated_file': str(out_path.relative_to(output_dir).as_posix()),
                        'cell_details': cell_details,
                    }
                    t['results'].append(result_item)

            except Exception as e:
                t['errors'].append(f'{filename}: {str(e)}')

            with tasks_lock:
                t['done'] = i + 1
                t['progress'] = int((i + 1) / len(selections) * 100)

        # Write Excel
        try:
            sorted_results = sorted(t['results'], key=lambda x: (x['filename'], x['scene']))
            excel_path = output_dir / 'summary.xlsx'
            write_excel(str(excel_path), sorted_results)
            t['excel_file'] = str(excel_path.relative_to(output_dir).as_posix())
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            t['errors'].append(f'Excel error: {str(e)}\\n{tb}')

        t['status'] = 'complete'
        t['progress'] = 100

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({'task_id': task_id})


@app.route('/progress/<task_id>')
def progress_stream(task_id):
    def generate():
        last_progress = -1
        while True:
            with tasks_lock:
                t = tasks.get(task_id)
            if t is None:
                yield f'data: {json.dumps({"status": "error", "msg": "Task not found"})}\n\n'
                break

            data = {
                'status': t['status'],
                'progress': t['progress'],
                'done': t['done'],
                'total': t['total'],
            }
            if t['status'] == 'complete':
                data['results'] = t['results']
                data['errors'] = t['errors']
                data['excel_file'] = t.get('excel_file', '')
                yield f'data: {json.dumps(data)}\n\n'
                break

            if t['progress'] != last_progress:
                yield f'data: {json.dumps(data)}\n\n'
                last_progress = t['progress']

            time.sleep(0.3)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/results/<task_id>')
def results_page(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
    if t is None:
        return 'Task not found', 404
    return render_template('results.html', task_id=task_id, results=t['results'],
                           errors=t['errors'], excel_file=t.get('excel_file', ''))


@app.route('/download/<task_id>/<path:filename>')
def download_file(task_id, filename):
    filepath = OUTPUT_DIR / task_id / filename
    if not filepath.exists():
        return 'File not found', 404
    return send_file(str(filepath), as_attachment=True)


@app.route('/download_zip/<task_id>')
def download_zip(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
    if t is None:
        return 'Task not found', 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        output_dir = OUTPUT_DIR / task_id / 'annotated'
        if output_dir.exists():
            for f in sorted(output_dir.iterdir()):
                zf.write(str(f), arcname=f.name)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'annotated_{task_id}.zip')


def _thumbnail(image, max_w=120, max_h=120):
    h, w = image.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image


@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'files' not in request.files:
        return jsonify({'error': 'No files provided'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400

    upload_dir = OUTPUT_DIR / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []
    for f in files:
        if f.filename and f.filename.lower().endswith('.czi'):
            save_path = upload_dir / f.filename
            counter = 1
            while save_path.exists():
                stem = Path(f.filename).stem
                ext = Path(f.filename).suffix
                save_path = upload_dir / f'{stem}_{counter}{ext}'
                counter += 1
            f.save(str(save_path))
            saved_paths.append(str(save_path))

    if not saved_paths:
        return jsonify({'error': 'No .czi files found in the upload'}), 400

    analyzed = []
    for sp in saved_paths:
        cf = Path(sp)
        try:
            data = read_czi(str(cf))
            channels = classify_channels(data['channel_metadata'], data['scenes'])
            previews = {}
            for s_name, s_data in data['scenes'].items():
                previews[s_name] = []
                for ch in s_data['channels']:
                    img_small = _thumbnail(ch['image'], max_w=120, max_h=120)
                    _, buf = cv2.imencode('.jpg', img_small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    b64 = base64.b64encode(buf).decode('utf-8')
                    previews[s_name].append({
                        'index': ch['index'],
                        'name': ch['name'],
                        'b64': b64,
                    })

            analyzed.append({
                'filepath': str(cf),
                'filename': cf.name,
                'scenes': list(data['scenes'].keys()),
                'channels': [
                    {'index': c.index, 'name': c.name, 'channel_type': c.channel_type, 'dye': c.dye}
                    for c in channels
                ],
                'previews': previews,
            })
        except Exception as e:
            analyzed.append({
                'filepath': str(cf),
                'filename': cf.name,
                'error': str(e),
            })

    return jsonify({'files': analyzed})

if __name__ == '__main__':
    # Auto-find available port
    import socket
    port = 5000
    for attempt in range(10):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            sock.close()
            break
        except OSError:
            sock.close()
            port += 1
    url = f'http://127.0.0.1:{port}'
    print(f' * Starting server on {url}')
    # Auto-open browser after a short delay
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)

