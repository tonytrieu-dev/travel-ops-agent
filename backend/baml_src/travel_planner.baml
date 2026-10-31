class ActivityOut {
    name: string,
    description: string,
    intensity: "low" | "moderate" | "high" @description("Match to the traveler's age and fitness."),
    source_url: string @description("A real URL from the activity research for this trip, never invented."),
}

class ItineraryDayOut {
    day_number: int,
    summary: string,
    activities: ActivityOut[],
}

class ItineraryOut {
    days: ItineraryDayOut[],
}

class ClarificationOut {
    questions: string[] @description(
        "Only for a genuinely ambiguous provided value, e.g. a destination name that could refer to multiple places.",
    ),
}

type PlannerLlmOutput = ItineraryOut | ClarificationOut;

// Cerebras is OpenAI-compatible; GenericClient requires an explicit base_url.
client Cerebras = openai.GenericClient.new(
  model = "gpt-oss-120b",
  base_url = "https://api.cerebras.ai/v1",
  api_key = env.CEREBRAS_API_KEY,
);

function PlanTrip(trip_details: string) -> PlannerLlmOutput {
    client: Cerebras
    prompt: `
        You are a travel agent. Plan a day-by-day itinerary from the traveler's origin,
        destination, dates, age, and fitness level.

        Plan directly from the details given. Only return questions when a value is genuinely
        ambiguous, such as a destination name that could mean more than one place. Do not ask about
        budget, interests, or other optional preferences; pick broadly popular choices instead.

        Pace the trip for the traveler's age and fitness. Older or less fit travelers get gentler
        activities, shorter days, and more rest; younger or fitter travelers can handle busier days.

        Ground every activity in the research you are given, and set its source_url to a real URL
        from that research. Treat flights as logistics, not activities.

        ${ctx.output_format()}

        Trip details:
        ${trip_details}
    `
}
