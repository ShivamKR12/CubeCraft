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
    NodePath, Vec3, Point3, TextNode, Texture, CardMaker,
    LColor, TextureStage, ClockObject, AudioSound
)

from noise import pnoise2
import math
import concurrent.futures
from queue import Queue
import logging
import functools
import os
import struct

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger(__name__)

SCALE       = 120.0   # Big, continent-scale features
OCTAVES     = 5       # Enough detail for peaks, not too noisy
PERSISTENCE = 0.45    # Middle ground—peaks still pronounced
LACUNARITY  = 2.0     # Standard “doubling” frequency 

CHUNK_SIZE = 8
RENDER_DISTANCE = 4
WORLD_HEIGHT = 32  # Maximum world height (for chunking)
MAX_FINALIZE_PER_FRAME = 1
MAX_DIRTY_PER_FRAME = 6
BLOCK_TYPES = {
    1: {'name': 'dirt',  'texture': 'assets/dirt.jpg'},
    2: {'name': 'grass', 'texture': 'assets/grass.jpg'},
    3: {'name': 'stone', 'texture': 'assets/stone.png'},
    4: {'name': 'sand',  'texture': 'assets/sand.png'},
    5: {'name': 'snow',  'texture': 'assets/snow.png'},
    6: {'name': 'cactus',  'texture': 'assets/cactus.png'},
    7: {'name': 'glass',  'texture': 'assets/glass.png'},
    8: {'name': 'oak_plank', 'texture': 'assets/oak_plank.png'},
    9: {'name': 'oak',    'texture': 'assets/oak.png'},
    10: {'name': 'leave', 'texture': 'assets/leave.png'},
    11: {'name': 'stone_brick', 'texture': 'assets/stone_brick.png'},
    12: {'name': 'grass2', 'texture': 'assets/grass2.png'},
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
    raw = pnoise2(x/scale, y/scale,
                  octaves=octaves,
                  persistence=persistence,
                  lacunarity=lacunarity)
    normalized = (raw + 1) * 0.5
    # make valleys broader and peaks sharper
    # normalized = ((raw + 1) * 0.5) ** 1.3
    max_h = WORLD_HEIGHT - 1
    return int(normalized * max_h)

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
                height = get_terrain_height(wx, wy, SCALE, OCTAVES, PERSISTENCE, LACUNARITY)
                block_type = None
                if wz > height:
                    continue
                elif wz == height:
                    if height >= 20:
                        block_type = 5  # snow
                    elif height >= 15:
                        block_type = 3  # stone
                    elif height >= 6:
                        block_type = 2  # grass
                    else:
                        block_type = 4  # sand
                elif wz < 2:
                    block_type = 3  # always stone below sea level
                else:
                    if height >= 15:
                        block_type = 3  # stone
                    elif height >= 6:
                        block_type = 1  # dirt
                    else:
                        block_type = 4  # more sand
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
                    height = get_terrain_height(wx, wy, SCALE, OCTAVES, PERSISTENCE, LACUNARITY)
                    block_type = None
                    if wz > height:
                        continue
                    elif wz == height:
                        if height >= 20:
                            block_type = 5  # snow
                        elif height >= 15:
                            block_type = 3  # stone
                        elif height >= 6:
                            block_type = 2  # grass
                        else:
                            block_type = 4  # sand
                    elif wz < 2:
                        block_type = 3  # always stone below sea level
                    else:
                        if height >= 15:
                            block_type = 3  # stone
                        elif height >= 6:
                            block_type = 1  # dirt
                        else:
                            block_type = 4  # more sand
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
            # skip “mined out” marker entries
            if block_type is None:
                continue
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
        # if self.world_blocks is not None:
        #     for pos in self.blocks:
        #         x, y, z = pos
        #         wx = self.chunk_x * CHUNK_SIZE + x
        #         wy = self.chunk_y * CHUNK_SIZE + y
        #         wz = self.chunk_z * CHUNK_SIZE + z
        #         if (wx, wy, wz) in self.world_blocks:
        #             del self.world_blocks[(wx, wy, wz)]
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
        self.app.globalClock = ClockObject.getGlobalClock()
        self.globalClock = self.app.globalClock
        self.player_vel = Vec3(0, 0, 0)
        self.is_on_ground = False
        self.app.accept("space", self.try_jump)
        self.no_clip = False
        self.app.accept("f", self.toggle_clip)     # press F to toggle
        self.app.taskMgr.add(self.update_camera, "cameraTask")

        # background music
        self.music = self.app.loader.loadMusic("assets/song_Forest.mp3")
        self.music.setLoop(True)
        self.music.setVolume(0.8)  # adjust volume to taste
        self.music.play()

        # Load footstep sounds (IDs must match BLOCK_TYPES)
        self.footstep_sounds = {
            1: self.app.loader.loadSfx("assets/step.ogg"),       # dirt
            2: self.app.loader.loadSfx("assets/step.ogg"),       # grass (uses same as dirt)
            4: self.app.loader.loadSfx("assets/sandStep.ogg"),   # sand
            5: self.app.loader.loadSfx("assets/snowStep.mp3"),   # snow
        }

        # Footstep timing
        self.step_timer = 0.0
        self.step_interval = 0.1  # seconds between step

        self.render_distance = RENDER_DISTANCE
    
    def play_footstep(self):
        print("▶play_footstep called") 
        # find the block directly under the player
        x, y, _ = self.app.camera.getPos()
        h = get_terrain_height(x, y, SCALE, OCTAVES, PERSISTENCE, LACUNARITY)
        print("play_footstep: player pos:", x, y, h)
        block_pos = (math.floor(x), math.floor(y), math.floor(h))
        print("play_footstep: block_pos:", block_pos)
        block_type = self.app.world_manager.world_blocks.get(block_pos)
        print("play_footstep: block_type:", block_type)
        sfx = self.footstep_sounds.get(block_type)
        print("play_footstep: sfx:", sfx)
        if not sfx:
            return
        
        # only start it if it’s not already playing
        if sfx.status() != AudioSound.PLAYING:
            print("sfx.status() != AudioSound.PLAYING:", sfx.status() != AudioSound.PLAYING)
            print("play_footstep: playing sound")
            sfx.setVolume(0.8)
            sfx.play()
    
    def toggle_clip(self):
        self.no_clip = not self.no_clip
        print("No-clip is now", self.no_clip)
        # if you want to “pause” gravity entirely, reset velocity:
        if self.no_clip:
            self.player_vel = Vec3(0,0,0)
            self.is_on_ground = False
            self.render_distance = 8  # increase render distance in no-clip mode
        else:
            self.render_distance = RENDER_DISTANCE

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
        fly_speed = 10
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
            # Choose speed: flycam vs. normal
            speed = fly_speed if self.no_clip else speed
            move *= speed * dt
        
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

        if self.no_clip:
            # simply move the camera with no gravity or collision
            cam.setPos(pos + move)
            return task.cont

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
        
        if abs(pos.z - round(pos.z)) < 0.05:
            self.is_on_ground = True
        
        moved = move.length() > 0 and not blocked_xy
        on_ground = self.is_on_ground

        # if moved:
        #     # play snow step every frame you move
        #     snd = self.footstep_sounds.get(5)   # 5 = snow
        #     if snd:
        #         snd.setVolume(10)             # pick a volume 0.0–1.0
        #         snd.play()

        cam.setPos(pos)

        # FOOTSTEP TIMER
        if moved and on_ground:
            print(f"→ footstep tick (pos={pos}, ground={on_ground}, moved={moved})")
            self.step_timer += dt
            print("step_timer:",self.step_timer)
            if self.step_timer >= self.step_interval:
                print("self.step_timer >= self.step_interval:",self.step_timer >= self.step_interval)
                self.step_timer = 0.0
                self.play_footstep()
        else:
            # reset when not moving or in air
            self.step_timer = 0.0

        if pos.z < -10:
            self.app.spawn_at_origin()
        
        return task.cont

class WorldManager:
    def __init__(self, app):
        self.app = app
        self.chunk_size = CHUNK_SIZE
        self.chunk_load_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.chunks_to_finalize = Queue()
        self.dirty_chunks = set()
        self.chunks = {}  # keys: (cx, cy, cz)
        self.world_blocks = {}  # keys: (wx, wy, wz)
        self.last_player_chunk = None
        # build the set of all (cx,cy,cz=0) around origin we want before spawning
        rd = self.app.player_controller.render_distance
        keys = [
            (dx, dy, 0)
            for dx in range(-rd, rd+1)
            for dy in range(-rd, rd+1)
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

        rd = self.app.player_controller.render_distance
        for dx in range(-rd, rd+1):
            for dy in range(-rd, rd+1):
                for dz in range(-rd, rd+1):
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

    # def finalize_chunks(self, task):
    #     count = 0
    #     while count < MAX_FINALIZE_PER_FRAME and not self.chunks_to_finalize.empty():
    #         cx, cy, cz, block_data = self.chunks_to_finalize.get()
    #         chunk = Chunk.from_block_data(self.app, 
    #                                       cx, cy, cz, 
    #                                       self.app.tex_dict, 
    #                                       block_data, 
    #                                       self.world_blocks)
    #         # enqueue for incremental mesh-building
    #         self.chunks[(cx, cy, cz)] = chunk
    #         self.app.building_chunks.append(chunk)

    #         # ─── patch in saved edits ───
    #         for (wx, wy, wz), bt in list(self.world_blocks.items()):
    #             # is this block inside the chunk we just created?
    #             local_x = wx - cx * CHUNK_SIZE
    #             local_y = wy - cy * CHUNK_SIZE
    #             local_z = wz - cz * CHUNK_SIZE
    #             if 0 <= local_x < CHUNK_SIZE and 0 <= local_y < CHUNK_SIZE and 0 <= local_z < CHUNK_SIZE:
    #                 # overwrite the perlin‐noise block with the saved one
    #                 chunk.blocks[(local_x, local_y, local_z)] = bt
    #         # override with any saved edits in this chunk:
    #         for (wx, wy, wz), bt in self.app.saved_blocks.items():
    #             sx, sy, sz = wx - cx*CHUNK_SIZE, wy - cy*CHUNK_SIZE, wz - cz*CHUNK_SIZE
    #             if 0 <= sx < CHUNK_SIZE and 0 <= sy < CHUNK_SIZE and 0 <= sz < CHUNK_SIZE:
    #                 # patch both the global map and chunk.local blocks:
    #                 self.world_blocks[(wx, wy, wz)] = bt
    #                 chunk.blocks[(sx, sy, sz)] = bt
    #         for (lx, ly, lz), btype in block_data.items():
    #             wx = cx * CHUNK_SIZE + lx
    #             wy = cy * CHUNK_SIZE + ly
    #             wz = cz * CHUNK_SIZE + lz
    #             self.world_blocks[(wx, wy, wz)] = btype
    #         # enqueue for incremental mesh‐building instead of building immediately
    #         self.chunks[(cx, cy, cz)] = chunk
    #         self.app.building_chunks.append(chunk)
    #         count += 1
    #     return task.cont

    def finalize_chunks(self, task):
        count = 0
        while count < MAX_FINALIZE_PER_FRAME and not self.chunks_to_finalize.empty():
            cx, cy, cz, block_data = self.chunks_to_finalize.get()

            # 1) Seed the global world map with this chunk’s base data
            for (lx, ly, lz), btype in block_data.items():
                wx, wy, wz = cx*CHUNK_SIZE + lx, cy*CHUNK_SIZE + ly, cz*CHUNK_SIZE + lz
                self.world_blocks[(wx, wy, wz)] = btype

            # 2) Build the chunk from that base data
            chunk = Chunk.from_block_data(
                self.app, cx, cy, cz, self.app.tex_dict, block_data, self.world_blocks
            )
            self.chunks[(cx, cy, cz)] = chunk
            self.app.building_chunks.append(chunk)

            # 3) Re-apply *every* saved edit into both global + chunk:
            for (wx, wy, wz), bt in self.app.saved_blocks.items():
                sx, sy, sz = wx - cx*CHUNK_SIZE, wy - cy*CHUNK_SIZE, wz - cz*CHUNK_SIZE
                if 0 <= sx < CHUNK_SIZE and 0 <= sy < CHUNK_SIZE and 0 <= sz < CHUNK_SIZE:
                    if bt is None:
                        # mined out — ensure both maps are empty
                        self.world_blocks.pop((wx, wy, wz), None)
                        chunk.blocks.pop((sx, sy, sz), None)
                    else:
                        # placed or replaced — write back in
                        self.world_blocks[(wx, wy, wz)] = bt
                        chunk.blocks[(sx, sy, sz)] = bt

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

        # Then *record* that this coordinate is now empty (so it stays empty on reload)
        self.app.saved_blocks[block_coord] = None

        # 4) Give the block to the player
        self.app.hotbar.add_block(block_type, 1)

        log.info(f"Mined {block_type} at {block_coord}")


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

        # And record it permanently:
        self.app.saved_blocks[place_pos] = block_type

        # 6) Consume the block from the player
        self.app.hotbar.remove_block(block_type, 1)

        log.info(f"Placed {block_type} at {place_pos}")

class Clouds:
    def __init__(self, app, height=100):
        self.app    = app
        self.height = height
        self.clouds_texStage = TextureStage("clouds")
        cm = CardMaker("clouds")
        cm.setFrame(-2048, 2048, -2048, 2048)
        self.node = app.render.attachNewNode(cm.generate())
        self.node.setP(-90)            # lie flat
        self.node.setZ(self.height)    # high in the sky
        self.node.setTwoSided(True)
        self.node.setTransparency(TransparencyAttrib.MAlpha)
        self.node.setDepthWrite(False)
        self.node.setBin("transparent", 10)
        self.node.setTexture(self.clouds_texStage, app.clouds_tex)
        self.node.setTexScale(self.clouds_texStage, 8, 8)

    def update(self, dt):
        # re-center on camera XY so it “follows” you
        cam = self.app.camera.getPos()
        self.node.setX(cam.x)
        self.node.setY(cam.y)
        # optional: animate texture scroll
        u_offset = (self.app.globalClock.getFrameTime() * 0.005) % 1.0
        self.node.setTexOffset(self.clouds_texStage, u_offset, 0)

class CubeCraft(ShowBase):
    def __init__(self):
        super().__init__()
        self.tex_dict = {}
        for k, info in BLOCK_TYPES.items():
            tex = self.loader.loadTexture(info['texture'])
            tex.setMagfilter(Texture.FTNearest)
            tex.setMinfilter(Texture.FTNearest)
            self.tex_dict[k] = tex
        
        self.clouds_tex = self.loader.loadTexture("assets/clouds.png")
        self.clouds_tex.setMagfilter(Texture.FTNearest)
        self.clouds_tex.setMinfilter(Texture.FTNearest)
        self.clouds_tex.clearRamMipmapImage(0)

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

        # tell Panda to fire "window-closed" when the user clicks the X:
        self.win.setCloseRequestEvent("window-closed")
        # catch that event and save+exit:
        self.accept("window-closed", self.exit_game)

        # track how many initial chunks have been meshed
        self.mesh_done = 0

        self.player_controller = PlayerController(self)
        self.ui_manager        = UIManager(self)

        # Ensure the loading frame is visible and drawn at least once
        self.ui_manager.loading_frame.show()
        self.graphicsEngine.renderFrame()
        self.graphicsEngine.renderFrame()

        # Before creating WorldManager, load any saved world:
        self.saved_blocks = self.load_world("world.dat")

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

        # Directional (sun) light
        self.directional_light = DirectionalLight('sun')
        self.directional_np    = self.render.attachNewNode(self.directional_light)
        self.directional_np.setHpr(0, -60, 0)    # starting angle
        self.render.setLight(self.directional_np)
        # self.render.setTwoSided(False)

        # Ambient light
        self.ambient_light = AmbientLight('ambient')
        self.ambient_np    = self.render.attachNewNode(self.ambient_light)
        self.ambient_light.setColor((0.4, 0.4, 0.4, 1))
        self.render.setLight(self.ambient_np)

        # ─── Day/Night Cycle Parameters ───
        self.day_length = 60.0      # seconds for a full day→night→day
        self.time_of_day = 0.0      # current time (0 … day_length)

        self.building_chunks = []
        self.taskMgr.add(self.block_interaction.update_ghost, "ghostBlockTask")
        self.taskMgr.add(self.update_daynight, "dayNightTask")
        self.clouds = Clouds(self, height=WORLD_HEIGHT*CHUNK_SIZE + 20)
        # add an update task
        self.taskMgr.add(self.update_clouds, "cloudsTask")
    
    def update_clouds(self, task):
        dt = self.globalClock.getDt()
        self.clouds.update(dt)
        return task.cont

    def update_chunk_building(self, task):
        # process more planes per frame if flycam
        max_planes = MAX_FINALIZE_PER_FRAME
        if self.player_controller.no_clip:
            max_planes = 10   # or however many you can handle

        planes = 0
        while self.building_chunks and planes < max_planes:
            chunk = self.building_chunks[0]
            still_more = chunk.process_next_plane()
            planes += 1

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

            for pos, bt in self.saved_blocks.items():
                # override global map
                self.world_manager.world_blocks[pos] = bt
                # find chunk & local coords
                (cx, cy, cz), (lx, ly, lz) = self.block_interaction.get_chunk_and_local(pos)
                chunk_key = (cx, cy, cz)
                # grab or create that chunk
                chunk = self.world_manager.chunks.get(chunk_key)
                if chunk is None:
                    # no chunk there yet — make a brand new shell and schedule it to build
                    chunk = Chunk(self, cx, cy, cz, self.tex_dict, self.world_manager.world_blocks)
                    self.world_manager.chunks[chunk_key] = chunk
                    self.building_chunks.append(chunk)
                # NOW it's guaranteed to be a real Chunk
                chunk.blocks[(lx, ly, lz)] = bt
                self.world_manager.dirty_chunks.add(chunk_key)
                # chunk = self.world_manager.chunks.get((cx, cy, cz))
                # if chunk:
                #     # override the chunk’s local storage
                #     chunk.blocks[(lx, ly, lz)] = bt
                #     # flag for rebuild
                #     self.world_manager.dirty_chunks.add((cx, cy, cz))

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
            self.accept("f2",     self.player_controller.toggle_clip)

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
        print("Saving and quitting...")
        # dump the world to disk
        self.save_world("world.dat")

        # then shut down threads and exit
        self.world_manager.chunk_load_executor.shutdown(wait=False)
        self.userExit()

    def set_block_type(self, k):
        self.block_interaction.selected_block_type = k

    def spawn_at_origin(self):
        x, y = 0, 0
        h = get_terrain_height(x, y, SCALE, OCTAVES, PERSISTENCE, LACUNARITY)
        spawn_z = h + PLAYER_HEIGHT + 10
        self.camera.setPos(x, y, spawn_z)
        self.player_controller.player_vel = Vec3(0, 0, 0)
        self.player_controller.is_on_ground = True

    def update_daynight(self, task):
        # Delta time
        dt = self.globalClock.getDt()
        # Advance and wrap
        self.time_of_day = (self.time_of_day + dt) % self.day_length
        # Compute a phase [0,2π)
        phase = (self.time_of_day / self.day_length) * 2 * math.pi

        # Example: sun height oscillates from -1 (midnight) to +1 (noon)
        sun_elevation = math.sin(phase)
        # Map to HPR pitch angle: -90° at midnight, +90° at noon
        pitch = sun_elevation * 90  

        # Rotate DirectionalLight node
        self.directional_np.setHpr(0, -pitch, 0)

        # Color interp: night = dark blue, day = warm white
        day_color = LColor(1.0, 0.95, 0.8, 1)    # soft daylight
        night_color = LColor(0.1, 0.1, 0.3, 1)   # deep night
        t = (sun_elevation + 1) / 2             # 0 at midnight, 1 at noon

        # Linear interpolate colors
        curr_color = day_color * t + night_color * (1 - t)
        self.directional_light.setColor(curr_color)

        # Ambient light: dimmer at night
        amb_day = LColor(0.4, 0.4, 0.5, 1)
        amb_night = LColor(0.05, 0.05, 0.1, 1)
        curr_amb = amb_day * t + amb_night * (1 - t)
        self.ambient_light.setColor(curr_amb)

        # Optional: background sky color
        sky_day = (0.5, 0.7, 1.0, 1)
        sky_night = (0.0, 0.0, 0.05, 1)
        curr_sky = tuple(sky_day[i] * t + sky_night[i] * (1 - t) for i in range(4))
        self.setBackgroundColor(*curr_sky)

        return task.cont
    
    def save_world(self, filename="world.dat"):
        """Serialize self.saved_blocks to a compact binary file, including mined blocks."""
        wb = self.saved_blocks
        print(f"Saving {len(wb)} saved edits...")
        with open(filename, "wb") as f:
            f.write(struct.pack("<I", len(wb)))  # number of entries
            for (x, y, z), bt in wb.items():
                # Encode `None` (mined blocks) as 255
                encoded_bt = 255 if bt is None else bt
                f.write(struct.pack("<iiiB", x, y, z, encoded_bt))
        print("World saved to", filename)

    def load_world(self, filename="world.dat"):
        """Return a dict of block edits, with None for mined blocks."""
        if not os.path.isfile(filename):
            return {}
        with open(filename, "rb") as f:
            data = f.read(4)
            if len(data) < 4:
                return {}
            (count,) = struct.unpack("<I", data)
            blocks = {}
            record_size = struct.calcsize("<iiiB")
            for _ in range(count):
                chunk = f.read(record_size)
                if len(chunk) < record_size:
                    break
                x, y, z, bt = struct.unpack("<iiiB", chunk)
                blocks[(x, y, z)] = None if bt == 255 else bt
            return blocks

if __name__ == "__main__":
    app = CubeCraft()
    app.run()
