# CubeCraft

A Minecraft-inspired voxel game built with Python and the Panda3D engine.

## Features

*   Infinite procedural world generation using Perlin noise.
*   Block mining and placing.
*   Simple inventory and hotbar system.
*   Day/night cycle with a moving sun and moon.
*   Dynamic lighting.
*   Player physics (gravity, jumping, collision).
*   World saving and loading.
*   Cloud layer.

## Installation & Running

1.  **Clone the repository:**
    ```bash
    git clone <your-repo-url>
    cd CubeCraft
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    # On Windows: venv\Scripts\activate
    # On macOS/Linux: source venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the game:**
    ```bash
    python main.py
    ```

## Controls

*   **W, A, S, D:** Move
*   **Mouse:** Look around
*   **Space:** Jump
*   **Left Click:** Mine block
*   **Right Click:** Place block
*   **1-9:** Select hotbar slot
*   **F:** Toggle No-clip/Fly mode
*   **F3:** Toggle debug information and wireframe
*   **Escape:** Pause/Resume game
