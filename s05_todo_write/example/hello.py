"""Greet someone by name."""


def greet(name: str) -> None:
    """Print a greeting to the given name.

    Args:
        name: The person to greet.

    """
    message = "Hello, " + name
    print(message)


if __name__ == "__main__":
    greet("Claude")
