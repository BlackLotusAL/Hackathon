package bot.sdk;

import bot.sdk.BoardTypes.Beam;
import java.util.List;

/** A snapshot taken before the two strategies act on this turn. */
public record TurnState(int turn, List<String> owner, List<String> contour, List<Beam> beams,
                        List<Integer> score, List<Integer> leadTurns, List<Integer> timeUs) {
    public TurnState {
        owner = List.copyOf(owner);
        contour = List.copyOf(contour);
        beams = List.copyOf(beams);
        score = List.copyOf(score);
        leadTurns = List.copyOf(leadTurns);
        timeUs = List.copyOf(timeUs);
    }
}
