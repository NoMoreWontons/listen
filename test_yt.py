"""_is_yt URL gate. Pure — no network. Run: python test_yt.py"""
import app


def demo():
    ok = ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
          "https://youtube.com/watch?v=abc123",
          "http://youtu.be/abc123",
          "https://www.youtube.com/shorts/xyz"]
    bad = ["https://vimeo.com/12345",
           "https://example.com/youtube.com/fake",
           "not a url at all",
           "",
           "javascript:alert(1)//youtu.be"]
    for u in ok:
        assert app._is_yt(u), f"should accept {u}"
    for u in bad:
        assert not app._is_yt(u), f"should reject {u}"
    print("test_yt: OK")


if __name__ == "__main__":
    demo()
