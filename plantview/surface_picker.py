"""Exact world-coordinate CAD picking with an instance BVH and cached cell trees.

A pointer move traverses only intersected BVH branches. Actor/data observers
invalidate the index after visibility, transforms or geometry change. Call
invalidate() after adding/removing model actors (the application loader does).
"""
from dataclasses import dataclass
import heapq
import itertools
import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy


@dataclass
class SurfaceHit:
    point: np.ndarray
    actor: object
    cell_id: int
    instance_id: int
    normal: np.ndarray
    kind: str = 'Face'


class BoundsTree:
    """Balanced BVH over N x 2 x 3 world bounds, including individual glyphs."""
    def __init__(self, bounds, leaf_size=8):
        self.bounds = np.asarray(bounds, dtype=np.float64).reshape((-1, 2, 3))
        self.nodes = []
        self.visited = 0
        if not len(self.bounds):
            return
        centers = self.bounds.mean(axis=1)

        def build(indices):
            lo = self.bounds[indices, 0].min(axis=0)
            hi = self.bounds[indices, 1].max(axis=0)
            index = len(self.nodes)
            self.nodes.append(None)
            if len(indices) <= leaf_size:
                self.nodes[index] = (lo, hi, indices, -1, -1)
            else:
                axis = np.ptp(centers[indices], axis=0).argmax()
                mid = len(indices) // 2
                indices = indices[np.argpartition(centers[indices, axis], mid)]
                left, right = build(indices[:mid]), build(indices[mid:])
                self.nodes[index] = (lo, hi, None, left, right)
            return index
        build(np.arange(len(self.bounds), dtype=np.int64))

    @staticmethod
    def interval(start, direction, lo, hi):
        parallel = abs(direction) < 1e-20
        if np.any(parallel & ((start < lo) | (start > hi))):
            return None
        near, far = np.full(3, -np.inf), np.full(3, np.inf)
        moving = ~parallel
        a = (lo[moving] - start[moving]) / direction[moving]
        b = (hi[moving] - start[moving]) / direction[moving]
        near[moving], far[moving] = np.minimum(a, b), np.maximum(a, b)
        lower, upper = max(0., float(near.max())), min(1., float(far.min()))
        return (lower, upper) if lower <= upper else None

    def candidates(self, start, end):
        """Yield near-to-far object bounds, without traversing missed subtrees."""
        self.visited = 0
        if not self.nodes:
            return
        direction = end - start
        first = self.interval(start, direction, *self.nodes[0][:2])
        if first is None:
            return
        queue = [(first[0], 0, 0)]
        while queue:
            near, kind, index = heapq.heappop(queue)
            if kind:
                yield near, index
                continue
            self.visited += 1
            _, _, indices, left, right = self.nodes[index]
            if indices is not None:
                for item in indices:
                    span = self.interval(start, direction, *self.bounds[item])
                    if span is not None:
                        heapq.heappush(queue, (span[0], 1, int(item)))
            else:
                for child in (left, right):
                    span = self.interval(start, direction, *self.nodes[child][:2])
                    if span is not None:
                        heapq.heappush(queue, (span[0], 0, child))


class SurfacePicker:
    def __init__(self, window):
        self.window = window
        self.cache = {}
        self._dirty = True
        self._observers = []
        self._entries = []
        self._tree = BoundsTree([])
        self._model_signature = None
        self._generation = 0
        self._last_key = self._last_hit = None
        self.last_stats = {'entries': 0, 'candidates': 0, 'nodes': 0, 'locator_builds': 0}

    def invalidate(self, *_):
        self._dirty = True
        self._last_key = None

    def clear(self):
        for obj, tag in self._observers:
            obj.RemoveObserver(tag)
        self._observers.clear()
        self.cache.clear()
        self._entries.clear()
        self._tree = BoundsTree([])
        self._model_signature = None
        self.invalidate()

    @staticmethod
    def _matrix(actor):
        m = actor.GetMatrix()
        return np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)])

    @staticmethod
    def _actor_signature(actor):
        m = actor.GetMatrix()
        mapper = actor.GetMapper()
        return (actor.GetVisibility(), actor.GetPickable(), m.GetMTime(),
                mapper.GetAddressAsString('') if mapper else '')

    def _watch_actor(self, actor):
        state = [self._actor_signature(actor)]
        def changed(*_):
            signature = self._actor_signature(actor)
            if signature != state[0]:
                state[0] = signature
                self.invalidate()
        self._observers.append((actor, actor.AddObserver('ModifiedEvent', changed)))
        transform = actor.GetUserTransform()
        if transform:
            self._observers.append((transform, transform.AddObserver('ModifiedEvent', self.invalidate)))

    def _watch_data(self, data, watched):
        key = data.GetAddressAsString('')
        if key in watched:
            return
        watched.add(key)
        self._observers.append((data, data.AddObserver('ModifiedEvent', self.invalidate)))
        points = data.GetPoints()
        if points:
            self._observers.append((points, points.AddObserver('ModifiedEvent', self.invalidate)))
            array = points.GetData()
            self._observers.append((array, array.AddObserver('ModifiedEvent', self.invalidate)))

    def prepare(self):
        """Build the index once. Cell trees are lazy and shared by instances."""
        models = self.window.models
        signature = tuple((id(m), id(m.get('actors')), len(m.get('actors', ()))) for m in models)
        if signature != self._model_signature:
            self._dirty = True
        if not self._dirty:
            return
        for obj, tag in self._observers:
            obj.RemoveObserver(tag)
        self._observers.clear()
        entries, bounds, watched, active_data = [], [], set(), set()
        for model in models:
            for actor in model.get('actors', ()):
                self._watch_actor(actor)
                mapper = actor.GetMapper()
                if mapper is None:
                    continue
                glyph = isinstance(mapper, vtk.vtkGlyph3DMapper)
                data = mapper.GetSource(0) if glyph else mapper.GetInput()
                if data is None or not hasattr(data, 'GetPoints'):
                    continue
                self._watch_data(data, watched)
                active_data.add(data.GetAddressAsString(''))
                cloud = mapper.GetInput() if glyph else None
                if cloud is not None:
                    self._watch_data(cloud, watched)
                if not actor.GetVisibility() or not actor.GetPickable() or not data.GetNumberOfCells():
                    continue
                matrix = self._matrix(actor)
                try:
                    inverse = np.linalg.inv(matrix)
                except np.linalg.LinAlgError:
                    continue
                b = np.asarray(data.GetBounds()).reshape((3, 2))
                if not np.isfinite(b).all() or np.any(b[:, 0] > b[:, 1]):
                    continue
                corners = np.array(list(itertools.product(*b)))
                offsets = vtk_to_numpy(cloud.GetPoints().GetData()) if cloud is not None and cloud.GetNumberOfPoints() else np.zeros((1, 3))
                # NavModel glyphs are translated copies: ScalingOff/OrientOff.
                corner_world = corners @ matrix[:3, :3].T
                origins = offsets @ matrix[:3, :3].T + matrix[:3, 3]
                low, high = corner_world.min(axis=0), corner_world.max(axis=0)
                base = dict(actor=actor, data=data, matrix=matrix, inverse=inverse)
                for i, (offset, origin) in enumerate(zip(offsets, origins)):
                    entries.append((base, np.array(offset, dtype=float), i if glyph else -1))
                    bounds.append((low + origin, high + origin))
        self._entries = entries
        self._tree = BoundsTree(bounds)
        self.cache = {k: v for k, v in self.cache.items() if k[0] in active_data}
        self._model_signature = signature
        self._dirty = False
        self._generation += 1
        self._last_key = None
        self.last_stats['entries'] = len(entries)

    def _locator(self, data):
        key = (data.GetAddressAsString(''), data.GetMTime())
        if key not in self.cache:
            self.cache = {k: v for k, v in self.cache.items() if k[0] != key[0]}
            locator = vtk.vtkStaticCellLocator()
            locator.SetDataSet(data)
            locator.BuildLocator()
            self.cache[key] = (locator, data)
            self.last_stats['locator_builds'] += 1
        return self.cache[key][0]

    def _visible(self, point):
        section = getattr(self.window, 'section_tool', None)
        return section is None or section.contains_point(point)

    def ray_pick(self, start, end):
        """Exact world-space ray pick (also useful for non-screen callers)."""
        self.prepare()
        start, end = np.asarray(start, float), np.asarray(end, float)
        direction = end - start
        length2 = float(np.dot(direction, direction))
        if length2 < 1e-24:
            return None
        best, best_t, candidates = None, float('inf'), 0
        for near, index in self._tree.candidates(start, end):
            if near > best_t + 1e-10:
                break
            base, offset, instance_id = self._entries[index]
            inverse = base['inverse']
            a = (inverse @ np.r_[start, 1.])[:3] - offset
            b = (inverse @ np.r_[end, 1.])[:3] - offset
            locator = self._locator(base['data'])
            points, ids, cell = vtk.vtkPoints(), vtk.vtkIdList(), vtk.vtkGenericCell()
            points.SetDataTypeToDouble()
            candidates += 1
            # Exact cells, including back surfaces retained after sectioning.
            locator.IntersectWithLine(a, b, 1e-9, points, ids, cell)
            for n in range(points.GetNumberOfPoints()):
                local = np.asarray(points.GetPoint(n)) + offset
                world = (base['matrix'] @ np.r_[local, 1.])[:3]
                t = float(np.dot(world - start, direction) / length2)
                if not 0. <= t <= min(1., best_t) or not self._visible(world):
                    continue
                cell_id = ids.GetId(n)
                base['data'].GetCell(cell_id, cell)
                if cell.GetCellDimension() != 2:
                    continue
                verts = np.array([cell.GetPoints().GetPoint(j) for j in range(cell.GetNumberOfPoints())])
                if len(verts) < 3:
                    continue
                normal = np.zeros(3)
                for j in range(1, len(verts) - 1):
                    normal += np.cross(verts[j] - verts[0], verts[j + 1] - verts[0])
                normal = inverse[:3, :3].T @ normal
                size = np.linalg.norm(normal)
                if size < 1e-15:
                    continue
                normal /= size
                best = SurfaceHit(world, base['actor'], int(cell_id), instance_id, normal)
                best.vertices = (verts + offset) @ base['matrix'][:3, :3].T + base['matrix'][:3, 3]
                best_t = t
        self.last_stats.update(candidates=candidates, nodes=self._tree.visited)
        return best

    def _display(self, point):
        renderer = self.window.plot.renderer
        renderer.SetWorldPoint(*point, 1.)
        renderer.WorldToDisplay()
        return np.asarray(renderer.GetDisplayPoint())

    def _snap(self, hit, x, y, pixels=7.):
        target, vertices = np.array([x, y]), hit.vertices
        displayed = np.array([self._display(p)[:2] for p in vertices])
        distances = np.linalg.norm(displayed - target, axis=1)
        vertex = int(distances.argmin())
        if distances[vertex] <= pixels and self._visible(vertices[vertex]):
            hit.point, hit.kind = vertices[vertex].copy(), 'Vértice'
            return hit
        best, distance = None, pixels
        for i in range(len(vertices)):
            a, b = displayed[i], displayed[(i + 1) % len(vertices)]
            ab = b - a
            length2 = np.dot(ab, ab)
            if length2 < 1e-16:
                continue
            t = np.clip(np.dot(target - a, ab) / length2, 0., 1.)
            dist = np.linalg.norm(a + ab * t - target)
            if dist < distance:
                # Preserve exact 3D edge coordinates under perspective.
                p, q = vertices[i], vertices[(i + 1) % len(vertices)]
                edge = q - p
                u = np.clip(np.dot(hit.point - p, edge) / max(np.dot(edge, edge), 1e-30), 0., 1.)
                point = p + edge * u
                if self._visible(point):
                    best, distance = point, dist
        if best is not None:
            hit.point, hit.kind = best, 'Aresta da malha'
        return hit

    def pick_hit(self, x, y, snap=False):
        self.prepare()
        renderer = self.window.plot.renderer
        camera = renderer.GetActiveCamera()
        section = getattr(self.window, 'section_tool', None)
        key = (float(x), float(y), bool(snap), self._generation,
               camera.GetMTime(), tuple(renderer.GetRenderWindow().GetSize()),
               getattr(section, 'revision', None))
        if key == self._last_key:
            return self._last_hit
        def world(z):
            renderer.SetDisplayPoint(x, y, z)
            renderer.DisplayToWorld()
            point = np.asarray(renderer.GetWorldPoint())
            return point[:3] / point[3]
        hit = self.ray_pick(world(0.), world(1.))
        if hit is not None and snap:
            hit = self._snap(hit, x, y)
        self._last_key, self._last_hit = key, hit
        return hit

    def pick(self, x, y):
        hit = self.pick_hit(x, y)
        return None if hit is None else hit.point.copy()
