package bot.sdk;

import bot.sdk.BoardTypes.Beam;
import bot.sdk.BoardTypes.NextBoost;
import bot.sdk.BoardTypes.Order;
import bot.sdk.BoardTypes.Point;
import java.util.List;
import java.util.Optional;

/** A snapshot taken before the two strategies act on this turn. */
public record TurnState(int turn, List<String> owner, List<String> contour, List<String> latent,
                        List<Beam> beams, List<Integer> score, List<Integer> bonus,
                        Optional<Order> order, Optional<Point> boost, Optional<NextBoost> boostNext) {
    public TurnState {
        owner = List.copyOf(owner);
        contour = List.copyOf(contour);
        latent = List.copyOf(latent);
        beams = List.copyOf(beams);
        score = List.copyOf(score);
        bonus = List.copyOf(bonus);
    }
}
