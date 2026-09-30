# Independent outputs

Create one broadcast session for a Moblin source, then add outputs on different enabled
relay nodes. Each output has its own unique YouTube key (manual mode) or dedicated
liveBroadcast/liveStream binding (API mode). Independent outputs may belong to the same
channel, subject to the actual account's API limits. API failures remain on their output.

Starting or stopping a publishing intent changes only that output. Batch actions return
one result per output; a failure does not imply that other accepted outputs failed.
Ending a YouTube event is a separate explicit operation. No stream is completed as part
of a route switch. In manual mode the operator manages the event in YouTube Studio.

One phone sends one H.264/AAC portrait stream to the ingress relay. Other relays receive
authenticated encrypted SRT copies over server links. Each relay uses an independent
FFmpeg publisher; there is no single `tee` command that can block every destination.
The control-plane service never handles encoded media bytes.

The loopback lab records three authenticated synthetic RTMP destinations, decodes every
recording, checks both codecs, 1080×1920, 30 fps, GOP ≤60, ≥90 video frames and ≥90 audio
packets, monotonic PTS/DTS and maximum packet gaps. Intersections of decoded frame hashes
verify the common video timeline. It then stops/restarts B and changes B to a rejected
destination, asserting source/A/C process identities and advancing frame counters survive.

Run `python -m scripts.broadcast_media_lab --mediamtx <binary> --ffmpeg <binary>
--ffprobe <binary> --directory <new-isolated-directory>`. Every run uses a new directory;
failed evidence is retained. No real YouTube endpoint is contacted: the lab's only
destination override is a private constructor argument restricted to loopback RTMP.
The production agent CLI has no such override. Linux CI also isolates the entire lab
with no external container network, a CPU/memory/PID ceiling and no added capabilities.
