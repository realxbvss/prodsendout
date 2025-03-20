from fastapi import FastAPI, File, UploadFile
import subprocess
import tempfile
import uuid

app = FastAPI()


@app.get("/")
async def health_check():
    return {"status": "OK"}


@app.post("/process")
async def process_video(
        audio: UploadFile = File(...),
        image: UploadFile = File(...)
):
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Сохранение файлов
            audio_path = f"{tmp_dir}/{uuid.uuid4()}.mp3"
            image_path = f"{tmp_dir}/{uuid.uuid4()}.jpg"

            with open(audio_path, "wb") as f:
                f.write(await audio.read())

            with open(image_path, "wb") as f:
                f.write(await image.read())

            # Генерация видео
            output_path = f"{tmp_dir}/output.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-loop", "1",
                "-i", image_path,
                "-i", audio_path,
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-shortest",
                output_path
            ]
            subprocess.run(cmd, check=True)

            return {"video_path": output_path}

    except Exception as e:
        return {"error": str(e)}