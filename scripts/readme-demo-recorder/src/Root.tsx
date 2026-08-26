import React from "react";
import { Composition } from "remotion";
import { SlackOnboarding } from "./SlackOnboarding";
import { FPS, DURATION } from "./theme";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="SlackOnboarding"
    component={SlackOnboarding}
    durationInFrames={DURATION}
    fps={FPS}
    width={1200}
    height={640}
  />
);
