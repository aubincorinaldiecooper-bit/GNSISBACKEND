import { Composition } from "remotion";
import { ASKS_FIRST_FRAMES, AsksFirst } from "./scenes/AsksFirst";
import { FILM_FRAMES, Film } from "./scenes/Film";
import { INTERRUPT_FRAMES, Interrupt } from "./scenes/Interrupt";
import { SAY_IT_FRAMES, SayIt } from "./scenes/SayIt";

const size = { width: 1920, height: 1080, fps: 30 };

export function Root() {
  return (
    <>
      <Composition id="say-it" component={SayIt} durationInFrames={SAY_IT_FRAMES} {...size} />
      <Composition id="asks-first" component={AsksFirst} durationInFrames={ASKS_FIRST_FRAMES} {...size} />
      <Composition id="gnsis-30s" component={Film} durationInFrames={FILM_FRAMES} {...size} />
      <Composition id="interrupt" component={Interrupt} durationInFrames={INTERRUPT_FRAMES} {...size} />
    </>
  );
}
