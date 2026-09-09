"""Reorder existing GIF frames forward/backward without resizing or redrawing."""
from pathlib import Path
from PIL import Image

assets = Path(__file__).resolve().parents[1] / 'assets'
with Image.open(assets / 'yuru-camp.gif') as original:
    frames, durations = [], []
    for i in range(original.n_frames):
        original.seek(i)
        # Composite transparent palette pixels before RGB conversion: their
        # hidden chroma-key color must not become visible in the new GIF.
        rgba = original.convert('RGBA')
        frame = Image.new('RGBA', rgba.size, 'white')
        frame.alpha_composite(rgba)
        frame = frame.convert('RGB')
        frame.info.clear()
        frames.append(frame)
        durations.append(original.info.get('duration', 100))
order = list(range(len(frames))) + list(range(len(frames)-2, 0, -1))
frames[0].save(assets / 'yuru-camp-pingpong.gif', save_all=True,
               append_images=[frames[i] for i in order[1:]],
               duration=[durations[i] for i in order], loop=0, disposal=2,
               optimize=False)
with Image.open(assets / 'yuru-camp-pingpong.gif') as check:
    assert check.size == frames[0].size
    assert check.info['loop'] == 0
    print(f'Original: {len(frames)} frames; sequence: {order}; saved: {check.n_frames} frames; size: {check.size}')
