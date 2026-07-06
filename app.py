import os
import io
import math
import cv2
import numpy as np
import requests
import uuid
import traceback
from flask import Flask, render_template, request, jsonify
from PIL import Image

app = Flask(__name__)

os.makedirs('models', exist_ok=True)
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

MODEL_PATH = "models/starry_night.t7"

# Model yükleme kontrolü
if os.path.exists(MODEL_PATH):
    print("-> Stil transfer modeli başarıyla yükleniyor...")
    net = cv2.dnn.readNetFromTorch(MODEL_PATH)
else:
    print(f"⚠️ UYARI: {MODEL_PATH} bulunamadı! Lütfen modeller klasörünü kontrol edin.")
    net = None

TILE_SIZE = 256
TILE_URL_TEMPLATE = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
MAP_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}


def latlon_to_global_pixel(lat_deg, lon_deg, zoom):
    """Enlem/boylam/zoom değerini, tüm dünya haritasının o zoom seviyesindeki
    global piksel koordinat sistemine (x, y) çevirir (Web Mercator projeksiyonu)."""
    siny = math.sin(math.radians(lat_deg))
    siny = min(max(siny, -0.9999), 0.9999)  # kutuplara çok yakın değerlerde log patlamasın diye sınırla

    x = TILE_SIZE * (0.5 + lon_deg / 360.0)
    y = TILE_SIZE * (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi))

    scale = 2 ** zoom
    return x * scale, y * scale


def fetch_map_for_bbox(north, south, east, west, zoom):
    """Verilen coğrafi sınırları (bbox) kapsayan tüm tile'ları indirir, birleştirir (stitch)
    ve tam olarak bbox'a denk gelen piksel alanını kırpıp (crop) OpenCV (BGR) formatında döner."""
    x_min, y_min = latlon_to_global_pixel(north, west, zoom)  # kuzey-batı köşesi
    x_max, y_max = latlon_to_global_pixel(south, east, zoom)  # güney-doğu köşesi

    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min

    tile_x_min = int(math.floor(x_min / TILE_SIZE))
    tile_x_max = int(math.floor(x_max / TILE_SIZE))
    tile_y_min = int(math.floor(y_min / TILE_SIZE))
    tile_y_max = int(math.floor(y_max / TILE_SIZE))

    canvas_width = (tile_x_max - tile_x_min + 1) * TILE_SIZE
    canvas_height = (tile_y_max - tile_y_min + 1) * TILE_SIZE
    canvas = Image.new('RGB', (canvas_width, canvas_height))

    for tx in range(tile_x_min, tile_x_max + 1):
        for ty in range(tile_y_min, tile_y_max + 1):
            url = TILE_URL_TEMPLATE.format(z=zoom, x=tx, y=ty)
            resp = requests.get(url, headers=MAP_HEADERS, timeout=15)
            if resp.status_code != 200:
                raise Exception(f"Harita karosu (tile) indirilemedi ({tx},{ty}): HTTP {resp.status_code}")
            tile_img = Image.open(io.BytesIO(resp.content)).convert('RGB')
            paste_x = (tx - tile_x_min) * TILE_SIZE
            paste_y = (ty - tile_y_min) * TILE_SIZE
            canvas.paste(tile_img, (paste_x, paste_y))

    # Kutunun tam sınırlarına denk gelen piksel penceresini kırp
    crop_left = int(round(x_min - tile_x_min * TILE_SIZE))
    crop_top = int(round(y_min - tile_y_min * TILE_SIZE))
    crop_right = int(round(x_max - tile_x_min * TILE_SIZE))
    crop_bottom = int(round(y_max - tile_y_min * TILE_SIZE))
    cropped = canvas.crop((crop_left, crop_top, crop_right, crop_bottom))

    # PIL (RGB) -> OpenCV (BGR) numpy dizisine çevir
    rgb_array = np.array(cropped)
    bgr_array = rgb_array[:, :, ::-1].copy()
    return bgr_array


def apply_style_transfer(img):
    if net is None:
        raise Exception("models/starry_night.t7 dosyası sunucuda bulunamadı veya yüklenemedi!")

    if img is None or img.size == 0:
        raise Exception("Harita resmi oluşturulamadı. Görsel boş veya bozuk.")

    img_resized = cv2.resize(img, (600, 600))
    (h, w) = img_resized.shape[:2]

    blob = cv2.dnn.blobFromImage(img_resized, 1.0, (w, h),
                                 (103.939, 116.779, 123.680),
                                 swapRB=False, crop=False)
    net.setInput(blob)
    
    try:
        output = net.forward()
    except cv2.error as cv_err:
        raise Exception(f"OpenCV Model İleri Besleme Hatası: {str(cv_err)}")

    output = output.reshape((3, output.shape[2], output.shape[3]))
    output[0] += 103.939
    output[1] += 116.779
    output[2] += 123.680
    
    output = np.clip(output, 0, 255)
    output = output.transpose(1, 2, 0).astype("uint8")
    return output

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/transform', methods=['POST'])
def transform():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Koordinat verileri backend'e ulaşmadı."}), 400
        
    north = data.get('north')
    south = data.get('south')
    east = data.get('east')
    west = data.get('west')
    zoom = data.get('zoom')

    if None in (north, south, east, west, zoom):
        return jsonify({"success": False, "error": "Harita sınır (bbox) verileri eksik."}), 400

    unique_id = uuid.uuid4().hex
    output_path = f"static/output_{unique_id}.png"

    try:
        # 🎯 GÜNCELLEME: Artık ekrandaki sabit ölçek kutusunun kapsadığı tam coğrafi
        # alan (bbox) frontend'den geliyor. Bu alanı kapsayan tüm harita tile'larını
        # (CartoDB Dark, anahtar gerektirmez) indirip birleştiriyor (stitch) ve tam
        # kutunun sınırlarına denk gelen piksel alanını kırpıyoruz.
        zoom_int = int(round(float(zoom)))
        map_image = fetch_map_for_bbox(float(north), float(south), float(east), float(west), zoom_int)

        # Stil transferini çalıştır
        art_image = apply_style_transfer(map_image)
        cv2.imwrite(output_path, art_image)

        return jsonify({"success": True, "art_url": "/" + output_path})

    except Exception as e:
        print("❌ DETAYLI BACKEND HATASI:")
        traceback.print_exc()

        return jsonify({"success": False, "error": f"Python Çalışma Zamanı Hatası: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)