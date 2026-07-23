import os, sys, json, threading, zipfile, io, time, base64, webbrowser, tempfile, re
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
from cell_counter import (read_czi, classify_channels, count_cells, annotate_image,
                          create_dead_mask, match_dead_cells, write_excel)

app = Flask(__name__,
    template_folder=str(BUNDLE_DIR / 'templates'),
    static_folder=str(BUNDLE_DIR / 'static'))
app.secret_key = os.urandom(24)

OUTPUT_DIR = APP_DIR / 'analysis_results'
OUTPUT_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = Path(tempfile.gettempdir()) / 'cell_counter_uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

EXCEL_FILENAME = "细胞统计汇总.xlsx"
ALL_CELLS_FILENAME = "所有细胞标识图.png"
DEAD_CELLS_FILENAME = "死细胞标识图.png"

# In-memory task store
tasks = {}
tasks_lock = threading.Lock()


def new_task_id():
    return datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]


def _safe_output_stem(filename):
    """Return a readable filename stem that is valid on Windows."""
    stem = Path(filename).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', stem).strip(' .')
    return (stem or 'CZI文件')[:120]


def _annotation_filenames(filename, used_stems):
    """Build a unique pair of annotation filenames for one input CZI."""
    base = _safe_output_stem(filename)
    stem = base
    suffix = 2
    while stem.casefold() in used_stems:
        stem = f'{base}_{suffix}'
        suffix += 1
    used_stems.add(stem.casefold())
    return (f'{stem}_{ALL_CELLS_FILENAME}',
            f'{stem}_{DEAD_CELLS_FILENAME}')


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
                    {'index': c.index, 'name': c.name, 'channel_type': c.channel_type,
                     'dye': c.dye, 'role': c.role}
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
        try:
            with tasks_lock:
                t = tasks[task_id]
            output_dir = OUTPUT_DIR / task_id
            output_dir.mkdir(parents=True, exist_ok=True)
            used_output_stems = set()

            for i, sel in enumerate(selections):
                filepath = sel["filepath"]
                total_ch = sel.get("total_channel", sel.get("live_channel", 0))
                dead_ch = sel.get("dead_channel")
                filename = Path(filepath).name
                annotated_filename, dead_annotated_filename = _annotation_filenames(
                    filename, used_output_stems)
                file_all_cell_panels = []
                file_dead_cell_panels = []

                try:
                    czi_data = read_czi(filepath)
                    for s_name, s_data in czi_data["scenes"].items():
                        total_img = None
                        dead_img = None
                        for ch in s_data["channels"]:
                            if ch["index"] == total_ch:
                                total_img = ch["image"]
                            if dead_ch is not None and ch["index"] == dead_ch:
                                dead_img = ch["image"]

                        if total_img is None:
                            with tasks_lock:
                                t["errors"].append(f"{filename} - {s_name}: total-cell channel not found")
                            continue

                        ch_info = None
                        for c in sel.get("channels_info", []):
                            if c["index"] == total_ch:
                                ch_info = c
                                break
                        ch_type = (ch_info or {}).get("channel_type", "fluorescence")
                        total_result = count_cells(
                            total_img, ch_type,
                            dye=(ch_info or {}).get("dye"), role="total")

                        dead_mask = None
                        dead_result = None
                        if dead_ch is not None and dead_img is not None:
                            dead_result = count_cells(
                                dead_img, "fluorescence", role="dead")
                            dead_mask = create_dead_mask(dead_result)

                        dead_labels = match_dead_cells(total_result, dead_result)
                        annotated = annotate_image(
                            total_img, total_result, dead_mask, dead_labels=dead_labels)

                        dead_count = len(dead_labels)
                        live_count = total_result["total"] - dead_count
                        viability = (live_count / total_result["total"] * 100) if total_result["total"] > 0 else 0

                        cell_details = []
                        for prop in total_result["props"]:
                            cell_details.append({
                                "label": prop["label"],
                                "area": prop["area"],
                                "circularity": prop["circularity"],
                                "mean_intensity": prop["mean_intensity"],
                                "is_dead": prop["label"] in dead_labels,
                            })

                        panel_name = f"{filename} - {s_name}"
                        file_all_cell_panels.append((panel_name, annotated))

                        result_item = {
                            "filename": filename,
                            "scene": s_name,
                            "channel_name": (ch_info or {}).get("name", "Total-cell channel"),
                            "channel_index": total_ch,
                            "channel_type": ch_type,
                            "dye": (ch_info or {}).get("dye", ""),
                            "total": total_result["total"],
                            "live": live_count,
                            "dead": dead_count,
                            "viability": viability,
                            "has_dead": dead_ch is not None,
                            "annotated_file": annotated_filename,
                            "dead_annotated_file": dead_annotated_filename,
                            "cell_details": cell_details,
                        }
                        with tasks_lock:
                            t["results"].append(result_item)

                        dead_background = dead_img if dead_img is not None else total_img
                        dead_annotated = annotate_image(
                            dead_background, total_result, dead_labels=dead_labels)
                        file_dead_cell_panels.append((panel_name, dead_annotated))

                    if file_all_cell_panels:
                        _write_png(output_dir / annotated_filename,
                                   _build_contact_sheet(file_all_cell_panels))
                        _write_png(output_dir / dead_annotated_filename,
                                   _build_contact_sheet(file_dead_cell_panels))

                except Exception as e:
                    with tasks_lock:
                        t["errors"].append(f"{filename}: {str(e)}")

                with tasks_lock:
                    t["done"] = i + 1
                    t["progress"] = int((i + 1) / len(selections) * 100)

            # One batch gets one workbook.  Each CZI has its own annotation
            # image pair in the same timestamp directory; no per-file Excel is
            # created.
            with tasks_lock:
                excel_path = output_dir / EXCEL_FILENAME
                try:
                    sorted_results = sorted(t["results"], key=lambda x: (x["filename"], x["scene"]))
                    write_excel(str(excel_path), sorted_results)
                    t["excel_file"] = EXCEL_FILENAME
                except Exception as e:
                    import traceback
                    import sys
                    tb = traceback.format_exc()
                    t["errors"].append(f"Excel error: {str(e)}\n{tb}")
                    t["excel_file"] = ""
                    print(f"[ExcelError] {tb}", file=sys.stderr, flush=True)

            with tasks_lock:
                t["status"] = "complete"
                t["progress"] = 100

        except Exception as e:
            import traceback
            import sys
            tb = traceback.format_exc()
            print(f"[FatalWorkerError] {tb}", file=sys.stderr, flush=True)
            with tasks_lock:
                tt = tasks.get(task_id)
                if tt is not None:
                    tt["status"] = "error"
                    tt["errors"].append(f"Fatal worker error: {str(e)}\n{tb}")


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
        output_dir = OUTPUT_DIR / task_id
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


def _write_png(path, image):
    """Write PNG data through imencode so Unicode Windows paths are reliable."""
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise RuntimeError(f'Unable to encode output image: {Path(path).name}')
    Path(path).write_bytes(encoded.tobytes())


def _build_contact_sheet(panels):
    """Build one output figure; multiple source images become a two-column grid."""
    if not panels:
        canvas = np.zeros((400, 800, 3), dtype=np.uint8)
        cv2.putText(canvas, 'No valid image result', (220, 210),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (220, 220, 220), 2, cv2.LINE_AA)
        return canvas
    if len(panels) == 1:
        return panels[0][1]

    columns = 2
    cell_w, image_h, title_h = 900, 675, 36
    cell_h = image_h + title_h
    rows = (len(panels) + columns - 1) // columns
    sheet = np.zeros((rows * cell_h, columns * cell_w, 3), dtype=np.uint8)
    for index, (_title, image) in enumerate(panels):
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        scale = min(cell_w / image.shape[1], image_h / image.shape[0])
        width = max(1, int(image.shape[1] * scale))
        height = max(1, int(image.shape[0] * scale))
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        row, column = divmod(index, columns)
        x0 = column * cell_w + (cell_w - width) // 2
        y0 = row * cell_h + title_h + (image_h - height) // 2
        sheet[y0:y0 + height, x0:x0 + width] = resized
        cv2.putText(sheet, f'Image {index + 1}',
                    (column * cell_w + 12, row * cell_h + 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
    return sheet


@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'files' not in request.files:
        return jsonify({'error': 'No files provided'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400

    upload_dir = UPLOAD_DIR

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
                    {'index': c.index, 'name': c.name, 'channel_type': c.channel_type,
                     'dye': c.dye, 'role': c.role}
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
    # Use one fixed port.  Silently moving to another port allowed several old
    # server versions to stay alive and made browser requests hit stale code.
    import socket
    port = 5000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', port))
    except OSError:
        print('ERROR: CellCounter is already running on http://127.0.0.1:5000')
        sys.exit(1)
    finally:
        sock.close()
    url = f'http://127.0.0.1:{port}'
    print(f' * Starting server on {url}')
    # Auto-open browser after a short delay
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)

