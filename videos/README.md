# GNSIS videos

Short silent loops that show how GNSIS works, made with [Remotion](https://www.remotion.dev)
and pieces from [Remocn](https://github.com/Remocn/remocn). They are for the landing page and
social posts. Same black, monochrome look and Geist type as the landing page.

| Clip | What it shows | Length |
| --- | --- | --- |
| `say-it` | You ask for a Groceries note. GNSIS opens Notes, makes and types the note, then checks the screen. | 10 s |
| `asks-first` | "Clear out my old drafts." GNSIS finds three files and asks before moving them to the Trash. | 10 s |
| `interrupt` | GNSIS is mid-sentence, you cut in, it stops and answers what you asked. | 9.3 s |
| `gnsis-30s` | The brand film: type, click, tap, then "Now, just say it." GNSIS, what it does in five beats, and "An omni agent, on your device." Silent for now; narration to come. | 30 s |

The scenes are illustrations, not screen recordings of the app.

## Run

```bash
npm install
npm run studio          # live preview in the browser
npm run render          # all clips to out/<id>.mp4
npm run render say-it   # one clip
```

Set `CHROME_PATH` to render with a browser you already have; otherwise Remotion downloads one.

## Layout

- `src/scenes/` one file per clip, with its timing at the top
- `src/ui/kit.tsx` shared parts: palette, fonts, status line, voice caption, window frame, typing
- `public/omni.webp` the glowing network image used in the film
- `src/remocn/` Remocn components copied in unchanged apart from a credit line (MIT)

## Licences

Remocn is MIT licensed. Remotion is free for individuals and companies of up to three people;
larger companies need a [Remotion company licence](https://www.remotion.dev/license).
