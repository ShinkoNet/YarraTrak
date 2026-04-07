from PIL import Image, ImageEnhance
import os

source_path = "yarratrak-transparent-full.png"
background_color = (41, 19, 129, 255)

print("Enhancing contrast and saturation...")
img = Image.open(source_path).convert("RGBA")

# Boost color saturation by 40%
enhancer_color = ImageEnhance.Color(img)
img = enhancer_color.enhance(1.4)

# Boost contrast by 25%
enhancer_contrast = ImageEnhance.Contrast(img)
img = enhancer_contrast.enhance(1.25)

print("Compositing splash onto navy background...")
background = Image.new("RGBA", img.size, background_color)
img = Image.alpha_composite(background, img)

resample_filter = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", 1))

img120 = img.resize((120, 120), resample_filter)
img120.save("logo_splash.png")

print("Success: Enhanced splash and menu icons saved.")
