"""Rasterize the same rounded geometry as favicon.svg into Windows icon sizes."""
from pathlib import Path
from PIL import Image, ImageDraw


def build():
    root = Path(__file__).resolve().parents[1] / 'src/cairn/server/static/assets'
    root.mkdir(parents=True, exist_ok=True)
    scale = 16
    image = Image.new('RGBA', (64 * scale, 64 * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(v * scale for v in (2, 2, 62, 62)), radius=15 * scale, fill='#575d99')
    for box, radius, color, angle in [((27, 13, 43, 23), 5, '#edbd87', 10),
                                      ((18, 27, 47, 37), 5, '#e4e4f5', -5),
                                      ((11, 41, 53, 52), 5.5, '#ffffff', 0)]:
        layer = Image.new('RGBA', image.size)
        ImageDraw.Draw(layer).rounded_rectangle(tuple(v * scale for v in box), radius=radius * scale, fill=color)
        center = ((box[0] + box[2]) * scale / 2, (box[1] + box[3]) * scale / 2)
        layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, center=center)
        image.alpha_composite(layer)
    image.resize((256, 256), Image.Resampling.LANCZOS).save(root / 'cairn.png')
    image.save(root / 'cairn.ico', sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


if __name__ == '__main__':
    build()
