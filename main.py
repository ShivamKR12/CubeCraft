from panda3d.core import loadPrcFile
loadPrcFile("configuration.prc")

# Now it’s safe to import the rest of Panda3D
from direct.showbase.ShowBase import ShowBase
from direct.gui.OnscreenImage import OnscreenImage
from direct.gui.OnscreenText import OnscreenText
from direct.gui.DirectGui import DirectFrame, DirectButton
from panda3d.core import (
    DirectionalLight, AmbientLight, WindowProperties,
    GeomVertexFormat, GeomVertexData, Geom, GeomNode,
    GeomTriangles, GeomVertexWriter, TransparencyAttrib,
    NodePath, Vec3, Point3, TextNode, Texture, CardMaker, LColor
)

from noise import pnoise2
import math
import concurrent.futures
from queue import Queue
import logging
import functools

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger(__name__)

SCALE = 10.0  
OCTAVES = 2  
PERSISTENCE = 0.5  
LUCANARITY = 2.0  

CHUNK_SIZE = 8
RENDER_DISTANCE = 4
WORLD_HEIGHT = 8  # Maximum world height (for chunking)
MAX_FINALIZE_PER_FRAME = 1
MAX_DIRTY_PER_FRAME = 6
BLOCK_TYPES = {
    1: {'name': 'dirt', 'texture': 'assets/dirt.jpg'},
    2: {'name': 'grass', 'texture': 'assets/grass.jpg'},
    3: {'name': 'stone', 'texture': 'assets/stone.png'},
}

PLAYER_HEIGHT = 1.75
PLAYER_RADIUS = 0.4
GRAVITY = 18
JUMP_VELOCITY = 7.0

HOTBAR_SLOT_COUNT = 9
HOTBAR_SLOT_SIZE = 0.12
HOTBAR_SLOT_PADDING = 0.015
HOTBAR_Y_POS = -0.88

FACES = [
    ((0, 1, 0),  "north",  [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)]),   # +Y
    ((0, -1, 0), "south",  [(1, 0, 0), (1, 0, 1), (0, 0, 1), (0, 0, 0)]),   # -Y
    ((-1, 0, 0), "west",   [(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)]),   # -X
    ((1, 0, 0),  "east",   [(1, 1, 0), (1, 1, 1), (1, 0, 1), (1, 0, 0)]),   # +X
    ((0, 0, 1),  "top",    [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]),   # +Z
    ((0, 0, -1), "bottom", [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]),   # -Z
]
FACE_UVS = [[(0, 0), (1, 0), (1, 1), (0, 1)] for _ in range(6)]

# NEIGHBOR_OFFSETS = [(-1,0,0),(1,0,0),(0,-1,0),(0,1,0),(0,0,-1),(0,0,1)]

def world_to_chunk_block(pos):
    cx = int(math.floor(pos[0] / CHUNK_SIZE))
    cy = int(math.floor(pos[1] / CHUNK_SIZE))
    cz = int(math.floor(pos[2] / CHUNK_SIZE))
    bx = int(pos[0] % CHUNK_SIZE)
    by = int(pos[1] % CHUNK_SIZE)
    bz = int(pos[2] % CHUNK_SIZE)
    return (cx, cy, cz), (bx, by, bz)

def get_terrain_height(x, y,
                       scale,
                       octaves,
                       persistence,
                       lacunarity):
    half = WORLD_HEIGHT // 2
    raw = pnoise2(x/scale, y/scale,
                  octaves=octaves,
                  persistence=persistence,
                  lacunarity=lacunarity)
    return int(raw * half + half)

class Chunk:
    def __init__(self, base, chunk_x, chunk_y, chunk_z, tex_dict, world_blocks):
        self.chunk_x = chunk_x
        self.chunk_y = chunk_y
        self.chunk_z = chunk_z
        self.base = base
        self.node = base.render.attachNewNode(f"chunk-{chunk_x}-{chunk_y}-{chunk_z}")
        self.blocks = {}  # (x, y, z): block_type, local coords
        self.tex_dict = tex_dict
        self.world_blocks = world_blocks
        self.pending_planes = [(z) for z in range(CHUNK_SIZE)]  # planes to build (z)

    @classmethod
    def from_block_data(cls, base, chunk_x, chunk_y, chunk_z, tex_dict, block_data, world_blocks):
        chunk = cls(base, chunk_x, chunk_y, chunk_z, tex_dict, world_blocks)
        chunk.blocks = block_data
        chunk.pending_planes = []
        return chunk

    def process_next_plane(self):
        if not self.pending_planes:
            return False
        z = self.pending_planes.pop(0)
        wz = self.chunk_z * CHUNK_SIZE + z
        for x in range(CHUNK_SIZE):
            wx = self.chunk_x * CHUNK_SIZE + x
            for y in range(CHUNK_SIZE):
                wy = self.chunk_y * CHUNK_SIZE + y
                height = get_terrain_height(wx, wy, SCALE, OCTAVES, PERSISTENCE, LUCANARITY)
                block_type = None
                if wz > height:
                    continue
                elif wz == height:
                    block_type = 2
                elif wz < 2:
                    block_type = 3
                else:
                    block_type = 1

                self.blocks[(x, y, z)] = block_type
                if self.world_blocks is not None:
                    self.world_blocks[(wx, wy, wz)] = block_type
        log.debug("Chunk %d,%d,%d plane %d generated", self.chunk_x,self.chunk_y,self.chunk_z, z)
        return bool(self.pending_planes)

    def is_ready(self):
        return not self.pending_planes

    @staticmethod
    def generate_blocks_data(chunk_x, chunk_y, chunk_z):
        blocks = {}
        for x in range(CHUNK_SIZE):
            wx = chunk_x * CHUNK_SIZE + x
            for y in range(CHUNK_SIZE):
                wy = chunk_y * CHUNK_SIZE + y
                for z in reversed(range(CHUNK_SIZE)):
                    wz = chunk_z * CHUNK_SIZE + z
                    height = get_terrain_height(wx, wy, SCALE, OCTAVES, PERSISTENCE, LUCANARITY)
                    block_type = None
                    if wz > height:
                        continue
                    elif wz == height:
                        block_type = 2
                    elif wz < 2:
                        block_type = 3
                    else:
                        block_type = 1

                    blocks[(x, y, z)] = block_type
        return blocks

    def build_mesh(self, force_cull=False):
        if force_cull:
            log.debug("CULL-pass → Rebuilding chunk %s", (self.chunk_x, self.chunk_y, self.chunk_z))
        if self.node.isEmpty():
            return
        self.node.node().removeAllChildren()
        mesh_data = {}
        idxs = {}
        for k in BLOCK_TYPES:
            fmt = GeomVertexFormat.getV3n3t2()
            vdata = GeomVertexData(f'chunk_{BLOCK_TYPES[k]["name"]}', fmt, Geom.UHStatic)
            mesh_data[k] = {
                'vdata': vdata,
                'vertex': GeomVertexWriter(vdata, 'vertex'),
                'normal': GeomVertexWriter(vdata, 'normal'),
                'texcoord': GeomVertexWriter(vdata, 'texcoord'),
                'triangles': GeomTriangles(Geom.UHStatic),
            }
            idxs[k] = 0

        for pos, block_type in self.blocks.items():
            x, y, z = pos
            wx = self.chunk_x * CHUNK_SIZE + x
            wy = self.chunk_y * CHUNK_SIZE + y
            wz = self.chunk_z * CHUNK_SIZE + z
            for face_idx, (face_dir, face_name, verts) in enumerate(FACES):
                nx, ny, nz = face_dir
                nwx, nwy, nwz = wx + nx, wy + ny, wz + nz
                # if face_name == "bottom":
                #     continue
                if force_cull:
                    if (nwx, nwy, nwz) in self.world_blocks:
                        continue
                if (nwx, nwy, nwz) not in self.world_blocks:
                    m = mesh_data[block_type]
                    idx = idxs[block_type]
                    for vert_idx, (vx, vy, vz) in enumerate(verts):
                        vwx = wx + vx
                        vwy = wy + vy
                        vwz = wz + vz
                        m['vertex'].addData3(vwx, vwy, vwz)
                        m['normal'].addData3(nx, ny, nz)
                        u, v_uv = FACE_UVS[face_idx][vert_idx]
                        m['texcoord'].addData2(u, v_uv)
                    m['triangles'].addVertices(idx, idx + 1, idx + 2)
                    m['triangles'].addVertices(idx, idx + 2, idx + 3)
                    m['triangles'].closePrimitive()
                    idxs[block_type] += 4

        for k in BLOCK_TYPES:
            if idxs[k] > 0:
                geom = Geom(mesh_data[k]['vdata'])
                geom.addPrimitive(mesh_data[k]['triangles'])
                node = GeomNode(f"chunk_mesh_{BLOCK_TYPES[k]['name']}")
                node.addGeom(geom)
                np = self.node.attachNewNode(node)
                np.setTexture(self.tex_dict[k])

    def destroy(self):
        if self.world_blocks is not None:
            for pos in self.blocks:
                x, y, z = pos
                wx = self.chunk_x * CHUNK_SIZE + x
                wy = self.chunk_y * CHUNK_SIZE + y
                wz = self.chunk_z * CHUNK_SIZE + z
                if (wx, wy, wz) in self.world_blocks:
                    del self.world_blocks[(wx, wy, wz)]
        self.node.removeNode()
        self.blocks.clear()

class PlayerController:
    def __init__(self, app):
        self.app = app
        self.key_map = {"w": False, "a": False, "s": False, "d": False}
        for key in self.key_map:
            self.app.accept(key, self.set_key, [key, True])
            self.app.accept(f"{key}-up", self.set_key, [key, False])
        self.sens = 0.2
        self.center_x = self.app.win.getXSize() // 2
        self.center_y = self.app.win.getYSize() // 2
        self.heading = 0
        self.pitch = 0
        from panda3d.core import ClockObject
        self.app.globalClock = ClockObject.getGlobalClock()
        self.globalClock = self.app.globalClock
        self.player_vel = Vec3(0, 0, 0)
        self.is_on_ground = False
        self.app.accept("space", self.try_jump)
        self.app.taskMgr.add(self.update_camera, "cameraTask")

    def set_key(self, key, value):
        self.key_map[key] = value

    def try_jump(self):
        if self.is_on_ground:
            self.player_vel.z = JUMP_VELOCITY
            self.is_on_ground = False

    def is_blocked_at(self, x, y, z):
        for dx in [-PLAYER_RADIUS, PLAYER_RADIUS]:
            for dy in [-PLAYER_RADIUS, PLAYER_RADIUS]:
                for dz in [0, PLAYER_HEIGHT]:
                    bx = int(math.floor(x + dx))
                    by = int(math.floor(y + dy))
                    bz = int(math.floor(z + dz) - 3)
                    if (bx, by, bz) in self.app.world_manager.world_blocks:
                        return True
        return False

    def update_camera(self, task):
        if self.app.paused:
            return task.cont
        dt = self.globalClock.getDt()
        cam = self.app.camera
        pos = cam.getPos()
        speed = 5.5
        move = Vec3(0, 0, 0)

        heading_rad = math.radians(self.heading)
        forward = Vec3(-math.sin(heading_rad), math.cos(heading_rad), 0)
        right = Vec3(math.sin(heading_rad + math.pi / 2), math.cos(heading_rad + math.pi / 2), 0)

        if self.key_map["w"]:
            move += forward
        if self.key_map["s"]:
            move -= forward
        if self.key_map["a"]:
            move -= right
        if self.key_map["d"]:
            move += right
        if move.length() > 0:
            move.normalize()
            move *= speed * dt

        self.player_vel.z -= GRAVITY * dt
        if self.player_vel.z < -GRAVITY:
            self.player_vel.z = -GRAVITY

        proposed = pos + move
        proposed.z += self.player_vel.z * dt

        self.is_on_ground = False

        next_xy = Vec3(proposed.x, proposed.y, pos.z)
        blocked_xy = self.is_blocked_at(next_xy.x, next_xy.y, next_xy.z)
        if not blocked_xy:
            pos.x, pos.y = next_xy.x, next_xy.y
        next_z = Vec3(pos.x, pos.y, proposed.z)
        blocked_z = self.is_blocked_at(next_z.x, next_z.y, next_z.z)
        if not blocked_z:
            pos.z = next_z.z
        else:
            if self.player_vel.z < 0:
                self.is_on_ground = True
                self.player_vel.z = 0
                pos.z = math.floor(pos.z + 0.01)
            elif self.player_vel.z > 0:
                self.player_vel.z = 0

        cam.setPos(pos)

        if pos.z < -10:
            self.app.spawn_at_origin()

        if self.app.mouseWatcherNode.hasMouse():
            md = self.app.win.getPointer(0)
            x = md.getX()
            y = md.getY()
            dx = x - self.center_x
            dy = y - self.center_y
            if dx != 0 or dy != 0:
                self.heading -= dx * self.sens
                self.pitch -= dy * self.sens
                self.pitch = max(-89, min(89, self.pitch))
                self.app.camera.setHpr(self.heading, self.pitch, 0)
                self.app.win.movePointer(0, self.center_x, self.center_y)
        return task.cont

class WorldManager:
    def __init__(self, app):
        self.app = app
        self.render_distance = RENDER_DISTANCE
        self.chunk_size = CHUNK_SIZE
        self.chunk_load_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.chunks_to_finalize = Queue()
        self.dirty_chunks = set()
        self.chunks = {}  # keys: (cx, cy, cz)
        self.world_blocks = {}  # keys: (wx, wy, wz)
        self.last_player_chunk = None
        # build the set of all (cx,cy,cz=0) around origin we want before spawning
        keys = [
            (dx, dy, 0)
            for dx in range(-RENDER_DISTANCE, RENDER_DISTANCE+1)
            for dy in range(-RENDER_DISTANCE, RENDER_DISTANCE+1)
        ]
        keys.sort(key=lambda k: math.hypot(k[0], k[1]))
        self.initial_queue = keys
        self.initial_total = len(keys)
        self.initial_done = 0
        self.initial_terrain_ready = False
        for key in self.initial_queue:
            cx,cy,cz = key
            fut = self.chunk_load_executor.submit(Chunk.generate_blocks_data, cx,cy,cz)
            fut.add_done_callback(lambda f, k=key: self._on_initial_chunk(k, f.result()))
            self.chunks[key] = None
        self.app.taskMgr.add(self.manage_chunks, "manageChunks")
        self.app.taskMgr.add(self.finalize_chunks, "finalizeChunks")
        self.app.taskMgr.add(self.process_dirty, "processDirty")

    def get_player_chunk_coords(self):
        cam = self.app.camera.getPos()
        chunk_x = int(math.floor(cam.x / self.chunk_size))
        chunk_y = int(math.floor(cam.y / self.chunk_size))
        chunk_z = int(math.floor(cam.z / self.chunk_size))
        return (chunk_x, chunk_y, chunk_z)
    
    def _on_initial_chunk(self, key, block_data):
        cx, cy, cz = key
        # enqueue for finalization
        self.chunks_to_finalize.put((cx, cy, cz, block_data))
        # remove from pending
        # self.initial_chunks_pending.remove(key)
        self.initial_done += 1
        # this fires after each chunk’s block data is generated…
        # still use the *full* total (gen + mesh) so we don't hide prematurely
        full_total = self.initial_total * 2
        self.app.ui_manager.update_loading(self.initial_done, full_total)
        # if *that was* the last one, mark ready
        # if not self.initial_chunks_pending:
        #     self.initial_terrain_ready = True
        if self.initial_done >= self.initial_total:
            self.initial_terrain_ready = True

    def manage_chunks(self, task):
        player_chunk = self.get_player_chunk_coords()
        max_cz = (WORLD_HEIGHT // CHUNK_SIZE) - 1
        min_cz = 0  # or set lower if you want caves below ground
        player_cx, player_cy, player_cz = player_chunk
        chunks_to_keep = set()

        for dx in range(-self.render_distance, self.render_distance+1):
            for dy in range(-self.render_distance, self.render_distance+1):
                for dz in range(-self.render_distance, self.render_distance+1):
                    cx = player_cx + dx
                    cy = player_cy + dy
                    cz = player_cz + dz
                    if cz < min_cz or cz > max_cz:
                        continue  # Don't generate below allowed range
                    key = (cx, cy, cz)
                    chunks_to_keep.add(key)
                    if key not in self.chunks:
                        future = self.chunk_load_executor.submit(
                            Chunk.generate_blocks_data, cx, cy, cz
                        )
                        # Register a callback that runs when the result is ready
                        callback = functools.partial(self._on_chunk_loaded, cx, cy, cz)
                        future.add_done_callback(callback)
                        self.chunks[key] = None

        for key, chunk in list(self.chunks.items()):
            if key not in chunks_to_keep and chunk is not None:
                chunk.destroy()
                del self.chunks[key]

        self.last_player_chunk = player_chunk
        return task.cont

    def finalize_chunks(self, task):
        count = 0
        while count < MAX_FINALIZE_PER_FRAME and not self.chunks_to_finalize.empty():
            cx, cy, cz, block_data = self.chunks_to_finalize.get()
            chunk = Chunk.from_block_data(self.app, 
                                          cx, cy, cz, 
                                          self.app.tex_dict, 
                                          block_data, 
                                          self.world_blocks)
            for (lx, ly, lz), btype in block_data.items():
                wx = cx * CHUNK_SIZE + lx
                wy = cy * CHUNK_SIZE + ly
                wz = cz * CHUNK_SIZE + lz
                self.world_blocks[(wx, wy, wz)] = btype
            # enqueue for incremental mesh‐building instead of building immediately
            self.chunks[(cx, cy, cz)] = chunk
            self.app.building_chunks.append(chunk)
            count += 1
        return task.cont

    def process_dirty(self, task):
        if self.app.paused:
            return task.cont
        # rebuild at most N dirty chunks per frame (to cap cost)
        count = 0
        # debug: show what's pending
        # log.debug("Dirty before rebuild: %s", self.dirty_chunks)
        while count < MAX_DIRTY_PER_FRAME and self.dirty_chunks:
            key = self.dirty_chunks.pop()
            log.debug("[dirty] → re-meshing chunk %s", key)
            chunk = self.chunks.get(key)
            if chunk is not None:
                chunk.build_mesh(force_cull=True)
            count += 1
        # log.debug("Dirty after rebuild: %s", self.dirty_chunks)
        return task.cont
    
    def _on_chunk_loaded(self, cx, cy, cz, future):
        result = future.result()
        self.chunks_to_finalize.put((cx, cy, cz, result))

class UIManager:
    def __init__(self, app):
        self.app = app

        # get current aspect ratio (width/height)
        self.ar = self.app.getAspectRatio()

        # whether F3‐debug is on
        self.debug_visible = False

        # ── Full-screen loading frame ──
        self.loading_frame = DirectFrame(
            frameColor=(0,0,0,1),
            frameSize=(-self.ar,self.ar,-1,1),
            parent=self.app.aspect2d
        )

        # hide gameplay UI until ready
        self.crosshair = OnscreenImage(
            image='assets/crosshair.png',
            pos=(0, 0, 0),
            scale=(0.05,0.05,0.05),
            parent=self.app.aspect2d
        )
        self.crosshair.setTransparency(TransparencyAttrib.MAlpha)
        self.loading_frame.show()
        self.crosshair.hide()
        # immediately flush one frame so the loading screen actually appears
        self.app.graphicsEngine.renderFrame()
        # Panda3D logo centered
        self.logo = OnscreenImage(
            image="assets/panda3d_logo_s_white.png",
            pos=(0, 0.2, 0),
            scale=(0.6, 0, 0.3),  # adjust as needed
            parent=self.loading_frame
        )
        self.logo.setTransparency(TransparencyAttrib.MAlpha)
        # Progress text
        self.loading_text = OnscreenText(
            text="Loading… 0%",
            pos=(0, -0.2),
            scale=0.08,
            fg=(1,1,1,1),
            align=TextNode.ACenter,
            parent=self.loading_frame,
            mayChange=True
        )

        self.debug_text = OnscreenText(
            text="", pos=(-1.3, 0.85), scale=0.04, fg=(1,1,1,1),
            align=TextNode.ALeft, mayChange=True, parent=self.app.aspect2d
        )
        self.debug_text.hide()

        self.slot_debug = False
        self.pause_frame = None

        # listen for window resize events
        self.app.accept("window-event", self.on_window_event)

        self.app.taskMgr.add(self.update_debug, "updateDebug")

    def on_window_event(self, wp):
        """Adjust full-screen frames to the new aspect ratio."""
        if wp != self.app.win.getProperties():
            # Panda sometimes sends multiple window-event args; safeguard
            wp = self.app.win.getProperties()

        w, h = wp.getXSize(), wp.getYSize()
        if h == 0:
            return  # avoid division by zero
        self.ar = w / h

        # update loading frame
        self.loading_frame['frameSize'] = (-self.ar, self.ar, -1, 1)

        # if you have other full-screen frames (e.g. the pause menu), update those too:
        if hasattr(self, 'pause_frame') and self.pause_frame:
            self.pause_frame['frameSize'] = (-self.ar, self.ar, -1, 1)

    def update_loading(self, done, total):
        # compute and clamp between 0 and 100
        if total > 0:
            raw_pct = int(done / total * 100)
            pct = max(0, min(100, raw_pct))
        else:
            pct = 0
        self.loading_text.setText(f"Loading… {pct}%")
        # if done >= total:
        #     # hide the loading frame, show gameplay HUD
        #     self.loading_frame.hide()
        #     self.crosshair.show()

    def toggle_debug(self):
        self.debug_visible = not self.debug_visible
        if self.debug_visible:
            self.debug_text.show()
        else:
            self.debug_text.hide()

    def update_debug(self, task):
        if self.app.paused:
            return task.cont
        if self.debug_visible:
            pos = self.app.camera.getPos()
            fps = self.app.globalClock.getAverageFrameRate()
            chunk = self.app.world_manager.get_player_chunk_coords()
            self.debug_text.setText(
                f"FPS: {fps:.1f}\n"
                f"Pos: ({pos.x:.2f}, {pos.y:.2f}, {pos.z:.2f})\n"
                f"Chunk: {chunk}\n"
                f"Block: {self.app.block_interaction.selected_block_type}"
            )
        return task.cont

    def show_pause_menu(self):
        if self.app.paused:
            return
        self.app.paused = True
        props = WindowProperties()
        props.setCursorHidden(False)
        props.setMouseMode(WindowProperties.M_absolute)
        self.app.win.requestProperties(props)
        self.pause_frame = DirectFrame(frameColor=(0,0,0,0.7), frameSize=(-self.ar,self.ar,-1,1), 
                                       parent=self.app.aspect2d)
        DirectButton(
            text="Resume", scale=0.1, pos=(0,0,0.2),
            command=self.app.hide_pause_menu, parent=self.pause_frame
        )
        DirectButton(
            text="Quit", scale=0.1, pos=(0,0,0),
            command=self.app.exit_game, parent=self.pause_frame
        )

    def hide_pause_menu(self):
        if not self.app.paused:
            return
        self.app.paused = False
        if self.pause_frame:
            self.pause_frame.destroy()
            self.pause_frame = None
        props = WindowProperties()
        props.setCursorHidden(True)
        props.setMouseMode(WindowProperties.M_confined)
        self.app.win.requestProperties(props)
        self.app.win.movePointer(0, self.app.player_controller.center_x, self.app.player_controller.center_y)

class HotbarManager:
    def __init__(self, app):
        self.app = app
        self.selected_index     = 0
        self.slot_assignments   = [None] * HOTBAR_SLOT_COUNT
        self.bg_nodes           = []
        self.frame_nodes        = []
        self.block_icons        = []
        self.slot_highlights    = []
        self.count_texts        = []

        # Root for easy hide/show
        self.root = NodePath("hotbar_root")
        self.root.reparentTo(self.app.aspect2d)

        total_width = HOTBAR_SLOT_COUNT * HOTBAR_SLOT_SIZE + (HOTBAR_SLOT_COUNT-1) * HOTBAR_SLOT_PADDING

        for i in range(HOTBAR_SLOT_COUNT):
            x = -total_width/2 + i*(HOTBAR_SLOT_SIZE + HOTBAR_SLOT_PADDING) + HOTBAR_SLOT_SIZE/2

            # Background
            cm = CardMaker(f"slot_bg_{i}")
            cm.setFrame(-HOTBAR_SLOT_SIZE/2, HOTBAR_SLOT_SIZE/2,
                        -HOTBAR_SLOT_SIZE/2, HOTBAR_SLOT_SIZE/2)
            bg = self.root.attachNewNode(cm.generate())
            bg.setPos(x, 0, HOTBAR_Y_POS)
            bg.setColor(LColor(0.2,0.2,0.2,0.8))
            bg.setTransparency(TransparencyAttrib.MAlpha)
            self.bg_nodes.append(bg)

            # Icon (starts hidden/transparent)
            icon = OnscreenImage(
                image="assets/transparent.png",
                pos=(x,0,HOTBAR_Y_POS),
                scale=(HOTBAR_SLOT_SIZE*0.8/2,1,HOTBAR_SLOT_SIZE*0.8/2),
                parent=self.root
            )
            icon.setTransparency(TransparencyAttrib.MAlpha)
            icon.hide()
            self.block_icons.append(icon)

            # Frame
            frame = OnscreenImage(
                image="assets/white_box.png",
                pos=(x,0,HOTBAR_Y_POS),
                scale=(HOTBAR_SLOT_SIZE/2,1,HOTBAR_SLOT_SIZE/2),
                parent=self.root
            )
            frame.setTransparency(TransparencyAttrib.MAlpha)
            self.frame_nodes.append(frame)

            # Highlight overlay
            hl = OnscreenImage(
                image="assets/white_box.png",
                pos=(x,0,HOTBAR_Y_POS),
                scale=(HOTBAR_SLOT_SIZE/2*1.1,1,HOTBAR_SLOT_SIZE/2*1.1),
                parent=self.root
            )
            hl.setTransparency(TransparencyAttrib.MAlpha)
            hl.setColor(1,1,0.2,0.5)
            hl.hide()
            self.slot_highlights.append(hl)

            # Count text
            ct = OnscreenText(
                text="", pos=(x + HOTBAR_SLOT_SIZE/4, HOTBAR_Y_POS - HOTBAR_SLOT_SIZE/3 + 0.01),
                scale=0.05, fg=(1,1,1,1), align=TextNode.ARight,
                mayChange=True, parent=self.root
            )
            ct.hide()
            ct.setBin('gui-popup', 50)
            self.count_texts.append(ct)

        # Select slot 0 by default
        self.select_slot(0)
        # Render initial (empty) UI
        self.update_ui()
        # after building all the UI nodes…
        self.root.hide()      # <-- add this line

    def _first_empty_slot(self):
        for i, bt in enumerate(self.slot_assignments):
            if bt is None:
                return i
        return None

    def _first_nonempty_slot(self):
        for i, bt in enumerate(self.slot_assignments):
            if bt is not None and self.app.inventory[bt] > 0:
                return i
        return None

    def select_slot(self, idx):
        idx %= HOTBAR_SLOT_COUNT
        for i, hl in enumerate(self.slot_highlights):
            if i == idx:
                hl.show()
            else:
                hl.hide()
        self.selected_index = idx

        # Update the BlockInteraction target
        bt = self.slot_assignments[idx]
        if bt is not None and self.app.inventory[bt] > 0:
            self.app.block_interaction.selected_block_type = bt
        else:
            self.app.block_interaction.selected_block_type = None

    def update_ui(self):
        """Redraw all slots from self.app.inventory & slot_assignments."""
        log.debug("[Hotbar] update_ui — inv: %s slots: %s", self.app.inventory, self.slot_assignments)
        for i, bt in enumerate(self.slot_assignments):
            if bt is not None and self.app.inventory[bt] > 0:
                # show icon
                tex = self.app.tex_dict[bt]
                self.block_icons[i].setImage(tex)
                self.block_icons[i].show()
                # show count
                self.count_texts[i].setText(str(self.app.inventory[bt]))
                self.count_texts[i].show()
            else:
                # empty slot
                self.block_icons[i].hide()
                self.count_texts[i].hide()
                self.slot_assignments[i] = None

        # If the currently selected slot is empty, jump to a non-empty one
        if self.slot_assignments[self.selected_index] is None:
            new_idx = self._first_nonempty_slot() or 0
            self.select_slot(new_idx)

    def add_block(self, block_type, amount=1):
        """Call when mining."""
        prev = self.app.inventory[block_type]
        self.app.inventory[block_type] = prev + amount

        if prev == 0:
            # new type: assign to first empty slot
            slot = self._first_empty_slot()
            if slot is not None:
                self.slot_assignments[slot] = block_type

        self.update_ui()

    def remove_block(self, block_type, amount=1):
        """Call when placing."""
        if self.app.inventory.get(block_type, 0) <= 0:
            return

        self.app.inventory[block_type] -= amount
        if self.app.inventory[block_type] <= 0:
            # emptied out: free that slot
            slot = self.slot_assignments.index(block_type)
            self.slot_assignments[slot] = None

        self.update_ui()

    def get_selected_blocktype(self):
        bt = self.slot_assignments[self.selected_index]
        if bt is not None and self.app.inventory[bt] > 0:
            return bt
        return None

    def has_block(self, block_type):
        return self.app.inventory.get(block_type, 0) > 0

    def show(self):   self.root.show()
    def hide(self):   self.root.hide()
    def destroy(self):self.root.removeNode()

class BlockInteraction:
    def __init__(self, app):
        self.app = app
        self.selected_block_type = None  # Now None at start!
        self.app.accept("mouse1", self.mine_block)
        self.app.accept("mouse3", self.place_block)
        self.ghost_np = self.app.render.attachNewNode("ghost")
        self.ghost_block = self.make_ghost_block()
        self.ghost_block.reparentTo(self.ghost_np)
        self.ghost_np.hide()
        self.app.taskMgr.add(self.update_ghost, "ghostBlockTask")

    def make_ghost_block(self):
        format = GeomVertexFormat.getV3n3()
        vdata = GeomVertexData('ghost', format, Geom.UHStatic)
        vertex = GeomVertexWriter(vdata, 'vertex')
        normal = GeomVertexWriter(vdata, 'normal')
        faces = [
            ((0, 1, 0),  [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)]),
            ((0, -1, 0), [(1, 0, 0), (1, 0, 1), (0, 0, 1), (0, 0, 0)]),
            ((-1, 0, 0), [(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)]),
            ((1, 0, 0),  [(1, 1, 0), (1, 1, 1), (1, 0, 1), (1, 0, 0)]),
            ((0, 0, 1),  [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]),
            ((0, 0, -1), [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]),
        ]
        triangles = GeomTriangles(Geom.UHStatic)
        idx = 0
        for nx, pts in faces:
            for px, py, pz in pts:
                vertex.addData3(px, py, pz)
                normal.addData3(*nx)
            triangles.addVertices(idx, idx + 1, idx + 2)
            triangles.addVertices(idx, idx + 2, idx + 3)
            triangles.closePrimitive()
            idx += 4
        geom = Geom(vdata)
        geom.addPrimitive(triangles)
        node = GeomNode('ghost_block')
        node.addGeom(geom)
        np = NodePath(node)
        np.setTransparency(TransparencyAttrib.MAlpha)
        np.setColor(1, 1, 1, 0.4)
        np.setDepthOffset(1)
        return np

    def cast_ray(self, max_dist=6.0, step=0.1):
        cam_pos = self.app.camera.getPos()
        dir_vec = self.app.camera.getQuat().getForward()
        pos = Point3(cam_pos)
        last_empty = None

        for _ in range(int(max_dist/step)):
            block = (
                int(math.floor(pos.x)),
                int(math.floor(pos.y)),
                int(math.floor(pos.z))
            )
            if block in self.app.world_manager.world_blocks:
                if last_empty is not None:
                    face_normal = (
                        block[0] - last_empty[0],
                        block[1] - last_empty[1],
                        block[2] - last_empty[2]
                    )
                else:
                    face_normal = (0,0,0)
                return block, face_normal, last_empty
            else:
                last_empty = block
            pos += dir_vec * step

        return None, None, None

    def update_ghost(self, task):
        if self.app.paused:
            return task.cont
        hit_block, face_normal, place_pos = self.cast_ray()
        if place_pos is not None:
            x,y,z = place_pos
            if 0 <= z < WORLD_HEIGHT and place_pos not in self.app.world_manager.world_blocks:
                self.ghost_np.setPos(x, y, z)
                self.ghost_np.show()
                return task.cont

        self.ghost_np.hide()
        return task.cont

    def get_chunk_and_local(self, world_pos):
        cx = int(math.floor(world_pos[0] / self.app.world_manager.chunk_size))
        cy = int(math.floor(world_pos[1] / self.app.world_manager.chunk_size))
        cz = int(math.floor(world_pos[2] / self.app.world_manager.chunk_size))
        lx = int(world_pos[0] % self.app.world_manager.chunk_size)
        ly = int(world_pos[1] % self.app.world_manager.chunk_size)
        lz = int(world_pos[2] % self.app.world_manager.chunk_size)
        return (cx, cy, cz), (lx, ly, lz)

    def get_chunks_to_update(self, world_pos, normal):
        chunk_key, local = self.get_chunk_and_local(world_pos)
        update = {chunk_key}
        for i, d in enumerate(['x','y','z']):
            if local[i] == 0 and normal[i] == -1:
                k = list(chunk_key)
                k[i] -= 1
                update.add(tuple(k))
            elif local[i] == self.app.world_manager.chunk_size - 1 and normal[i] == 1:
                k = list(chunk_key)
                k[i] += 1
                update.add(tuple(k))
        return update

    def mine_block(self):
        log.info("Mine block triggered")
        if self.app.paused:
            log.warning("Can't mine: game paused")
            return

        block_coord, normal, _ = self.cast_ray()
        if not block_coord:
            return

        wm = self.app.world_manager
        block_type = wm.world_blocks.get(block_coord)
        if block_type is None:
            return

        # 1) Remove the block from the world map
        del wm.world_blocks[block_coord]

        # 2) Remove from the chunk’s local storage
        chunk_key, local = self.get_chunk_and_local(block_coord)
        chunk = wm.chunks.get(chunk_key)
        if chunk:
            chunk.blocks.pop(local, None)

        wm.dirty_chunks.add(chunk_key)

        # 4) Give the block to the player
        self.app.hotbar.add_block(block_type, 1)


    def place_block(self):
        # 1) Pick from hotbar
        block_type = self.app.hotbar.get_selected_blocktype()
        if block_type is None or not self.app.hotbar.has_block(block_type):
            return

        # 2) Ray-cast for the empty position
        _, normal, place_pos = self.cast_ray()
        if not place_pos:
            return

        wm = self.app.world_manager
        if place_pos in wm.world_blocks:
            return

        # 3) Add the block to the world map
        wm.world_blocks[place_pos] = block_type

        # 4) Add to the chunk’s local storage
        chunk_key, local = self.get_chunk_and_local(place_pos)
        chunk = wm.chunks.get(chunk_key)
        if chunk:
            chunk.blocks[local] = block_type

        wm.dirty_chunks.add(chunk_key)

        # 6) Consume the block from the player
        self.app.hotbar.remove_block(block_type, 1)

class CubeCraft(ShowBase):
    def __init__(self):
        super().__init__()
        self.tex_dict = {}
        for k, info in BLOCK_TYPES.items():
            tex = self.loader.loadTexture(info['texture'])
            tex.setMagfilter(Texture.FTNearest)
            tex.setMinfilter(Texture.FTNearest)
            self.tex_dict[k] = tex

        # ─────── NEW ───────
        # Master inventory counts (block_type → count)
        self.inventory = {bt: 0 for bt in BLOCK_TYPES}
        # ───────────────────

        self.disableMouse()
        self.camera.setHpr(0, 0, 0)
        props = WindowProperties()
        props.setCursorHidden(True)
        props.setMouseMode(WindowProperties.M_relative)
        self.win.requestProperties(props)
        self.paused = True
        self.spawn_done = False

        # track how many initial chunks have been meshed
        self.mesh_done = 0

        self.player_controller = PlayerController(self)
        self.ui_manager        = UIManager(self)

        # Ensure the loading frame is visible and drawn at least once
        self.ui_manager.loading_frame.show()
        self.graphicsEngine.renderFrame()
        self.graphicsEngine.renderFrame()

        self.world_manager     = WorldManager(self)
        self.block_interaction = BlockInteraction(self)
        self.hotbar            = HotbarManager(self)

        self.ui_manager.update_loading(0, self.world_manager.initial_total * 2)

        self.pause_frame = None
        self.accept("escape", self.handle_escape_key)
        self.accept("f3", self.toggle_f3_features)
        for k in BLOCK_TYPES:
            self.accept(str(k), self.set_block_type, [k])

        # Add hotbar number key bindings:
        for i in range(HOTBAR_SLOT_COUNT):
            self.accept(str(i+1), lambda idx=i: self.hotbar.select_slot(idx))

        self.taskMgr.add(self.update_chunk_building, "updateChunkBuilding")

        light = DirectionalLight('light')
        light_np = self.render.attachNewNode(light)
        light_np.setHpr(0, -60, 0)
        self.render.setLight(light_np)
        self.render.setTwoSided(False)
        ambient = AmbientLight("ambient")
        ambient.setColor((0.4, 0.4, 0.4, 1))
        ambient_np = self.render.attachNewNode(ambient)
        self.render.setLight(ambient_np)

        self.building_chunks = []
        self.taskMgr.add(self.block_interaction.update_ghost, "ghostBlockTask")

    def update_chunk_building(self, task):
        # Even while paused, build one plane of the next chunk each frame
        if self.building_chunks:
            chunk = self.building_chunks[0]
            still_more = chunk.process_next_plane()
            if not still_more:
                # initial mesh (no culling) now that all planes exist
                log.debug("Chunk %d,%d,%d built (unculled mesh).", chunk.chunk_x, chunk.chunk_y, chunk.chunk_z)
                chunk.build_mesh(force_cull=False)

                # now count this mesh as “done”
                self.mesh_done += 1
                # update combined progress
                done = self.world_manager.initial_done + self.mesh_done
                total = self.world_manager.initial_total * 2
                self.ui_manager.update_loading(done, total)

                # mark neighbors for cull-pass
                # for dx, dy, dz in NEIGHBOR_OFFSETS:
                #     neighbor_key = (chunk.chunk_x + dx,
                #                     chunk.chunk_y + dy,
                #                     chunk.chunk_z + dz)
                #     self.world_manager.dirty_chunks.add(neighbor_key)

                # now let process_dirty handle the force_cull pass over subsequent frames
                self.world_manager.dirty_chunks.add((chunk.chunk_x,
                                                     chunk.chunk_y,
                                                     chunk.chunk_z))
                # done—drop it from the queue
                self.building_chunks.pop(0)

        # Only spawn once *every* mesh and cull‐remesh is fully finished:
        done  = self.world_manager.initial_done  + self.mesh_done
        total = self.world_manager.initial_total * 2
        if (not self.spawn_done
            and done >= total
            and not self.building_chunks):
            # and not self.world_manager.dirty_chunks):
            log.info(">>> World load complete — unpausing now")

            # 1) hide the loading screen and flush it
            self.ui_manager.loading_frame.hide()
            self.ui_manager.crosshair.show()
            # now that we’re truly in‐game, show the hotbar
            self.hotbar.show()
            self.graphicsEngine.renderFrame()

            # 2) place the camera and re‐enable controls
            self.spawn_at_origin()
            self.spawn_done = True
            self.paused = False

            # drop the startup no-ops:
            self.ignore("mouse1"); self.ignore("mouse3")
            # bind mining/placing
            self.accept("mouse1", self.block_interaction.mine_block)
            self.accept("mouse3", self.block_interaction.place_block)
            # bind movement keys
            for k in ["w","a","s","d"]:
                self.accept(k,     lambda key=k: self.player_controller.set_key(key, True))
                self.accept(f"{k}-up", lambda key=k: self.player_controller.set_key(key, False))
            # bind jump
            self.accept("space", self.player_controller.try_jump)
            # bind escape & F3
            self.accept("escape", self.handle_escape_key)
            self.accept("f3",     self.toggle_f3_features)

        return task.cont

    def handle_escape_key(self):
        if not self.paused:
            self.ui_manager.show_pause_menu()
        else:
            self.ui_manager.hide_pause_menu()

    def hide_pause_menu(self):
        self.ui_manager.hide_pause_menu()

    def toggle_f3_features(self):
        self.toggle_wireframe()
        self.ui_manager.toggle_debug()

    def exit_game(self):
        self.world_manager.chunk_load_executor.shutdown(wait=False)
        self.userExit()

    def set_block_type(self, k):
        self.block_interaction.selected_block_type = k

    def spawn_at_origin(self):
        x, y = 0, 0
        h = get_terrain_height(x, y, SCALE, OCTAVES, PERSISTENCE, LUCANARITY)
        spawn_z = h + PLAYER_HEIGHT + 10
        self.camera.setPos(x, y, spawn_z)
        self.player_controller.player_vel = Vec3(0, 0, 0)
        self.player_controller.is_on_ground = True

if __name__ == "__main__":
    app = CubeCraft()
    app.run()
