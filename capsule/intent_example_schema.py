"""The typed shape of one intent example line (row 569 item 6), called by `intent.lint`."""
from capsule import row_schemas


def problems(example, at):
    """One `<at>: intent_example.v1 <path>: <message>` per violation of the example line's schema."""
    return [f"{at}: intent_example.v1 {e}" for e in row_schemas.problems("intent_example.v1", example)]
