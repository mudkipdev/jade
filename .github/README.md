# jade
An interactive CLI video editor.

## Requirements
- ffmpeg
- ffprobe

## Syntax
```
gameplay:
    add "~/Videos/2026-09-27_09-44-20.mp4" 03:30 03:45
    add "~/Videos/2026-09-27_09-39-48.mp4" 06:40 06:50
    speed 2x
    mute

add gameplay
add "~/Videos/outro.mp4"
compress 20mb
save "~/Videos/output.mp4"
```