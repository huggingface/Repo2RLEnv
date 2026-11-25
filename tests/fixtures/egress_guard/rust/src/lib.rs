//! Dependency-bearing fixture for the Rust solve-time controls.

/// Render an integer as a decimal string.
pub fn render(n: i32) -> String {
    let mut buffer = itoa::Buffer::new();
    buffer.format(n).to_string()
}
