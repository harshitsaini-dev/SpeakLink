import React from "react";

/**
 * Which Broadcast recording the application is playing, and the intent behind
 * choosing it.
 *
 * WHY THIS LIVES ABOVE THE ROUTES
 *
 * The player used to be owned by Broadcast History, which meant it died the
 * moment the operator clicked Receiver Status - audio stopped, position lost,
 * and a second visit started the download again. A recording an operator is
 * listening to is not a property of the page they happen to be on.
 *
 * The provider only holds WHICH recording is active and whether the operator
 * asked for it to start. The audio element itself lives in the single player
 * rendered by Layout, which survives route changes because Layout does.
 *
 * WHY THERE IS AN "INTENT" AT ALL
 *
 * Selecting a recording and playing one used to be two separate actions, so
 * the History Play button opened the bar and the operator then had to press
 * the footer's own Play. One click on something labelled Play has to mean
 * play. The intent travels with the selection, and the player starts playback
 * once the audio is genuinely ready.
 */

const RecordingPlaybackContext = React.createContext(null);

export function RecordingPlaybackProvider({ children }) {
  const [active, setActive] = React.useState(null);
  // Bumped on every explicit play request, so asking for the SAME recording
  // again is still an event the player can act on.
  const [playToken, setPlayToken] = React.useState(0);
  // Raised when a live broadcast starts. The player pauses rather than
  // closing, so the operator can resume afterwards.
  const [pauseToken, setPauseToken] = React.useState(0);

  const playRecording = React.useCallback((session) => {
    if (!session) return;
    setActive((current) => (current && current.id === session.id
      ? current : session));
    setPlayToken((value) => value + 1);
  }, []);

  const stopPlayback = React.useCallback(() => {
    setActive(null);
  }, []);

  /**
   * Pause without forgetting what was selected.
   *
   * Used when a live broadcast starts: a recording coming out of the HQ
   * speakers can be picked up by the HQ microphone and go out over the
   * announcement. Visiting the Console is not enough to trigger this - only
   * actually going live is.
   */
  const pauseForBroadcast = React.useCallback(() => {
    setPauseToken((value) => value + 1);
  }, []);

  /** A recording that no longer exists must not keep playing from memory. */
  const forgetRecording = React.useCallback((sessionId) => {
    setActive((current) => (current && current.id === sessionId ? null : current));
  }, []);

  const value = React.useMemo(() => ({
    active, playToken, pauseToken,
    playRecording, stopPlayback, pauseForBroadcast, forgetRecording,
  }), [active, playToken, pauseToken,
       playRecording, stopPlayback, pauseForBroadcast, forgetRecording]);

  return (
    <RecordingPlaybackContext.Provider value={value}>
      {children}
    </RecordingPlaybackContext.Provider>
  );
}

export function useRecordingPlayback() {
  const value = React.useContext(RecordingPlaybackContext);
  if (!value) {
    // A row that cannot reach the provider would silently do nothing when
    // clicked, which is worse than failing loudly during development.
    throw new Error(
      "useRecordingPlayback must be used inside a RecordingPlaybackProvider");
  }
  return value;
}

/**
 * The same context, but tolerant of not being there.
 *
 * Broadcasting does not depend on a recording player existing, and saying it
 * does would make BroadcastProvider untestable on its own. This lets the
 * safety pause happen when the player IS mounted and be a no-op when it is
 * not.
 */
export function useOptionalRecordingPlayback() {
  const value = React.useContext(RecordingPlaybackContext);
  return value || {
    active: null, playToken: 0, pauseToken: 0,
    playRecording: () => {}, stopPlayback: () => {},
    pauseForBroadcast: () => {}, forgetRecording: () => {},
  };
}

export default RecordingPlaybackContext;
