"""Optional acceptance run on a supplied model, using the real Qt import workflow.

python tests/verify_real_model.py MODEL --output DIRECTORY [--layer TEXT]
No model names or component handles are built into this check.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plantview.gpu import configure_surface, gpu_report
configure_surface()
from PySide6 import QtCore, QtWidgets
from plantview.app import Window
from plantview.branding import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--layer', default='')
    parser.add_argument('--bounds', type=float, nargs=6)
    parser.add_argument('--view', type=float, nargs=3, default=[1, 1, 1])
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication([])
    window = Window()
    window.resize(1600, 950)
    window.show()
    started = time.monotonic()
    result = {'version': VERSION, 'source': str(args.model.resolve())}
    finishing = False

    def finish():
        result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        result['gpu'] = gpu_report(window.plot)
        result['actors'] = sum(len(model['actors']) for model in window.models)
        result['log'] = window.log.toPlainText()
        window.plot.render()
        window.screen().grabWindow(int(window.winId())).save(str(args.output / 'interface.png'))
        window.plot.screenshot(str(args.output / 'viewport.png'))
        (args.output / 'validation.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        window.dirty = False
        window.close()
        app.exit(0 if result.get('loaded') else 1)

    def observe():
        nonlocal finishing
        if finishing:
            return
        if time.monotonic() - started > args.timeout:
            result['loaded'] = False
            result['error'] = 'Acceptance run exceeded its time limit'
        elif window.worker is not None or window.queue:
            return
        else:
            result['loaded'] = bool(window.models)
            actors = [actor for model in window.models for actor in model['actors']]
            matches = [actor for actor in actors if args.layer.casefold() in
                       str(getattr(actor, '_plantview_layer', '')).casefold()]
            if args.layer and not matches:
                result['loaded'] = False
                result['error'] = 'Requested layer was not imported'
            window.plot.view_vector(args.view, viewup=(0, 0, 1))
            if args.bounds:
                window.plot.reset_camera(bounds=args.bounds)
                result['focus_bounds'] = args.bounds
            elif args.layer and matches:
                bounds = [min(a.bounds[i] for a in matches) if i % 2 == 0 else
                          max(a.bounds[i] for a in matches) for i in range(6)]
                window.plot.reset_camera(bounds=bounds)
                result['focus_bounds'] = list(bounds)
            else:
                window.plot.reset_camera()
            window.tree.expandToDepth(0)
        finishing = True
        timer.stop()
        QtCore.QTimer.singleShot(1000, finish)

    timer = QtCore.QTimer()
    timer.timeout.connect(observe)
    window.enqueue([{'path': str(args.model.resolve()), 'visible': True, 'scale': .001}])
    timer.start(100)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
