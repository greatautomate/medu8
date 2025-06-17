import os
import re
import asyncio
import tempfile
import shutil
import aiohttp
import subprocess
from hydrogram import Client, filters
from hydrogram.types import Message
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Telegram file size limit (2GB)
TG_MAX_SIZE = 2 * 1024 * 1024 * 1024

# Regex for .m3u8 and direct video URLs
M3U8_REGEX = re.compile(r'https?://[^\s]+\.m3u8')
VIDEO_REGEX = re.compile(r'https?://[^\s]+(?:\.mp4|\.mov|\.webm|\.mkv|\.avi|\.flv|\.ts)(?:\?[^\s]*)?')

app = Client("video_merge_bot", bot_token=BOT_TOKEN)

HELP_TEXT = (
    "👋 *Welcome!*\n\n"
    "Send me an `.m3u8` playlist URL or a direct video download link.\n"
    "I'll download, merge, and upload the video (max 2GB).\n\n"
    "*Commands:*\n"
    "/start - Show welcome message\n"
    "/help - Show this help\n"
)

async def download_file(session, url, dest, progress_cb=None):
    async with session.get(url) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get('content-length', 0))
        downloaded = 0
        with open(dest, 'wb') as f:
            async for chunk in resp.content.iter_chunked(1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    await progress_cb(downloaded, total)
    return dest

async def download_m3u8(session, m3u8_url, workdir, progress_cb=None):
    # Download playlist
    async with session.get(m3u8_url) as resp:
        resp.raise_for_status()
        playlist = await resp.text()
    # Parse segment URLs
    base_url = m3u8_url.rsplit('/', 1)[0]
    segments = []
    for line in playlist.splitlines():
        if line and not line.startswith('#'):
            seg_url = line if line.startswith('http') else f"{base_url}/{line}"
            segments.append(seg_url)
    # Download segments
    seg_files = []
    total = len(segments)
    for idx, seg_url in enumerate(segments):
        seg_path = os.path.join(workdir, f"seg_{idx:05d}.ts")
        await download_file(session, seg_url, seg_path)
        seg_files.append(seg_path)
        if progress_cb:
            await progress_cb(idx + 1, total)
    return seg_files

async def merge_segments_ffmpeg(segment_files, output_path):
    # Create ffmpeg input file list
    list_path = output_path + ".txt"
    with open(list_path, 'w') as f:
        for seg in segment_files:
            f.write(f"file '{seg}'\n")
    # Merge with ffmpeg
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", output_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"ffmpeg failed: {stderr.decode()}")
    os.remove(list_path)
    return output_path

async def handle_video_upload(message, video_path):
    size = os.path.getsize(video_path)
    if size > TG_MAX_SIZE:
        await message.reply("❌ The merged video exceeds Telegram's 2GB limit. Cannot upload.")
        return
    await message.reply_video(video_path, caption="✅ Here is your merged video!")

@app.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await message.reply(HELP_TEXT, parse_mode="markdown")

@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply(HELP_TEXT, parse_mode="markdown")

@app.on_message(filters.private | filters.group | filters.channel)
async def handle_message(client, message: Message):
    text = message.text or ""
    m3u8_match = M3U8_REGEX.search(text)
    video_match = VIDEO_REGEX.search(text)
    url = m3u8_match.group(0) if m3u8_match else (video_match.group(0) if video_match else None)
    if not url:
        return  # Ignore unrelated messages

    status_msg = await message.reply("🔗 Processing your request...")

    try:
        with tempfile.TemporaryDirectory() as workdir:
            async with aiohttp.ClientSession() as session:
                if url.endswith('.m3u8'):
                    # Download and merge segments
                    await status_msg.edit("📥 Downloading playlist and segments...")
                    seg_files = []
                    async def seg_progress(done, total):
                        await status_msg.edit(f"📥 Downloading segments: {done}/{total}")
                    seg_files = await download_m3u8(session, url, workdir, seg_progress)
                    await status_msg.edit("🔄 Merging segments with ffmpeg...")
                    merged_path = os.path.join(workdir, "output.mp4")
                    await merge_segments_ffmpeg(seg_files, merged_path)
                else:
                    # Direct video download
                    await status_msg.edit("📥 Downloading video file...")
                    merged_path = os.path.join(workdir, "output.mp4")
                    async def file_progress(done, total):
                        percent = int(done / total * 100) if total else 0
                        await status_msg.edit(f"📥 Downloading: {percent}%")
                    await download_file(session, url, merged_path, file_progress)
                # Check file size
                size = os.path.getsize(merged_path)
                if size > TG_MAX_SIZE:
                    await status_msg.edit("⚠️ The merged video exceeds Telegram's 2GB limit. Cannot upload.")
                    return
                await status_msg.edit("⬆️ Uploading to Telegram...")
                await message.reply_video(merged_path, caption="✅ Here is your video!")
                await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    app.run()
