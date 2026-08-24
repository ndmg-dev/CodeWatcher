"""
make_icon.py — gera icon.ico do Code Watcher.

Rode uma vez (ou de novo se quiser mudar o design) com:
    python make_icon.py

Motivo de nao usar make_icon_image() de watcher_gui.py direto: aquela
funcao desenha o icone da bandeja (pequeno, 64px, precisa ficar legivel a
16px). O icone do executavel e visto maior (barra de tarefas, atalho,
Explorer em icones grandes) e vale a pena ter mais detalhe: gradiente,
sombra suave, e um brilho no "olho" para nao ficar um circulo chapado.
"""

import math

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024  # desenha grande, o .ico guarda varias resolucoes reduzidas


def lerp(a, b, t):
    return a + (b - a) * t


def make_base():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # --- sombra suave por baixo do disco -----------------------------------
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    pad = 60
    sd.ellipse([pad, pad + 26, SIZE - pad, SIZE - pad + 26], fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(34))
    img.alpha_composite(shadow)

    # --- disco de fundo com gradiente radial escuro -------------------------
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    cx = cy = SIZE / 2
    r = SIZE / 2 - pad
    steps = 160
    top = (26, 30, 38)      # #1a1e26
    bottom = (15, 17, 21)   # #0f1115
    for i in range(steps, 0, -1):
        t = i / steps
        rad = r * t
        color = tuple(int(lerp(bottom[c], top[c], t)) for c in range(3))
        bd.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=color + (255,))
    img.alpha_composite(bg)

    # --- aro externo (contorno vivo, verde da marca) -------------------------
    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    ring_w = 26
    accent = (74, 222, 128)  # #4ade80
    rd.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent + (255,), width=ring_w)
    img.alpha_composite(ring)

    # --- "olho" estilizado: lente (vesica piscis) via intersecao de 2 circulos
    eye_half_w = r * 0.62
    lens_r = r * 0.92
    offset = lens_r * 0.42  # quanto os 2 circulos se afastam do centro

    circle_a = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(circle_a).ellipse(
        [cx - offset - lens_r, cy - lens_r, cx - offset + lens_r, cy + lens_r], fill=255)
    circle_b = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(circle_b).ellipse(
        [cx + offset - lens_r, cy - lens_r, cx + offset + lens_r, cy + lens_r], fill=255)

    from PIL import ImageChops
    lens_mask = ImageChops.multiply(circle_a, circle_b)
    # a lente pode ficar mais alta que o eye_half_w desejado; corta os
    # cantos de cima/baixo com uma faixa retangular para achatar o formato
    band = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(band).rectangle(
        [0, cy - eye_half_w, SIZE, cy + eye_half_w], fill=255)
    lens_mask = ImageChops.multiply(lens_mask, band)

    eye_layer = Image.new("RGBA", (SIZE, SIZE), accent + (255,))
    img.paste(eye_layer, (0, 0), lens_mask)

    # iris escura no centro
    iris_r = eye_half_w * 0.52
    idraw = ImageDraw.Draw(img)
    idraw.ellipse([cx - iris_r, cy - iris_r, cx + iris_r, cy + iris_r],
                  fill=(15, 17, 21, 255))

    # pupila com leve gradiente + brilho especular (canto superior esquerdo)
    pupil_r = iris_r * 0.5
    for i in range(40, 0, -1):
        t = i / 40
        rad = pupil_r * t
        c = tuple(int(lerp(10, 30, t)) for _ in range(3))
        idraw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=c + (255,))

    glint_r = pupil_r * 0.32
    gx, gy = cx - pupil_r * 0.42, cy - pupil_r * 0.42
    idraw.ellipse([gx - glint_r, gy - glint_r, gx + glint_r, gy + glint_r],
                  fill=(255, 255, 255, 235))

    small_glint_r = glint_r * 0.4
    gx2, gy2 = cx + pupil_r * 0.35, cy + pupil_r * 0.15
    idraw.ellipse([gx2 - small_glint_r, gy2 - small_glint_r,
                   gx2 + small_glint_r, gy2 + small_glint_r],
                  fill=(255, 255, 255, 130))

    return img


def main():
    img = make_base()
    out = "icon.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(out, sizes=sizes)
    print(f"gerado: {out}")


if __name__ == "__main__":
    main()
