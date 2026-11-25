use egressfixture::render;

#[test]
fn renders_decimal() {
    assert_eq!(render(42), "42");
}
