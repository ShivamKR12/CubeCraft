from direct.showbase.ShowBase import ShowBase
from direct.task import Task
from panda3d.core import WindowProperties, ClockObject, Vec3
import math

GRAVITY = 9.8
JUMP_VELOCITY = 5.0
PLAYER_RADIUS = 0.3
PLAYER_HEIGHT = 1.8

class PlayerController:
    def __init__(self, app):
        self.app = app
        self.key_map = {"w": False, "a": False, "s": False, "d": False}
        for key in self.key_map:
            app.accept(key, self.set_key, [key, True])
            app.accept(f"{key}-up", self.set_key, [key, False])
        self.heading = 0
        self.pitch = 0
        self.sens = 0.2
        self.center_x = app.win.getXSize() // 2
        self.center_y = app.win.getYSize() // 2
        self.globalClock = ClockObject.getGlobalClock()
        self.player_vel = Vec3(0, 0, 0)
        self.is_on_ground = False
        self.snow_sounds = [app.loader.loadSfx("assets/step.ogg") for _ in range(4)]
        self.next_sound = 0
        app.taskMgr.add(self.update, "update-task")

    def set_key(self, key, value):
        self.key_map[key] = value

    def play_snow(self):
        snd = self.snow_sounds[self.next_sound]
        snd.setVolume(0.3)
        snd.play()
        self.next_sound = (self.next_sound + 1) % len(self.snow_sounds)

    def update(self, task):
        dt = self.globalClock.getDt()
        cam = self.app.camera
        pos = cam.getPos()
        move = Vec3(0,0,0)
        rad = math.radians(self.heading)
        forward = Vec3(-math.sin(rad), math.cos(rad), 0)
        right = Vec3(forward.y, -forward.x, 0)
        if self.key_map['w']: move += forward
        if self.key_map['s']: move -= forward
        if self.key_map['a']: move -= right
        if self.key_map['d']: move += right
        moved = move.length() > 0
        speed = 5.0
        if moved:
            move.normalize()
            move *= speed * dt
            new_pos = pos + move
            cam.setPos(new_pos)
            print("▶ Playing snow step")
            self.play_snow()
        if self.app.mouseWatcherNode.hasMouse():
            md = self.app.win.getPointer(0)
            dx = md.getX() - self.center_x
            dy = md.getY() - self.center_y
            if dx or dy:
                self.heading -= dx * self.sens
                self.pitch = max(-89, min(89, self.pitch - dy * self.sens))
                cam.setHpr(self.heading, self.pitch, 0)
                self.app.win.movePointer(0, self.center_x, self.center_y)
        return Task.cont

class MREApp(ShowBase):
    def __init__(self):
        super().__init__()
        self.disableMouse()
        props = WindowProperties()
        props.setCursorHidden(True)
        props.setMouseMode(WindowProperties.M_relative)
        self.win.requestProperties(props)
        self.loader.loadModel('models/environment').reparentTo(self.render)
        self.controller = PlayerController(self)

if __name__ == '__main__':
    app = MREApp()
    app.run()
