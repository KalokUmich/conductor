use super::*;

#[test]
fn test_multiply() {
    assert_eq!(multiply(3, 4), 12);
}

#[test]
fn test_multiply_zero() {
    let result = multiply(0, 5);
    assert_eq!(result, 0);
}

#[tokio::test]
async fn test_async_multiply() {
    assert!(multiply(2, 3) > 0);
}
