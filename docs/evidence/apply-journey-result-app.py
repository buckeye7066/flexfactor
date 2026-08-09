def add(a, b):
    """Adds two numbers and returns their sum."""
    return a + b

def buggy_div(a, b):
    if b == 0:
        raise ValueError("Division by zero error")
    else:
        return a / b
