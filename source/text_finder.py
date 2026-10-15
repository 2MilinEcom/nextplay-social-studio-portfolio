import re

def find_words_in_transcript(words, transcript):
    """
    Returns a set of words from `words` that are found in the `transcript`.
    - words: list of strings (keywords to search for)
    - transcript: string (the full transcript text)
    """
    transcript_lower = transcript.lower()
    found = set()
    for word in words:
        if word.lower() in transcript_lower:
            found.add(word)
    return found

def find_words_with_sentences(words, transcript):
    """
    Returns a dict mapping each found word to a list of sentences in which it appears.
    - words: list of strings (keywords to search for)
    - transcript: string (the full transcript text)
    """
    # Split transcript into sentences (simple split on . ! ?)
    sentences = re.split(r'(?<=[.!?])\s+', transcript)
    result = {}
    for word in words:
        found_sentences = []
        word_lower = word.lower()
        for sentence in sentences:
            if word_lower in sentence.lower():
                found_sentences.append(sentence.strip())
        if found_sentences:
            result[word] = found_sentences
    return result

if __name__ == "__main__":
    keywords = ["strapless bra"]
    transcript = {"VIDEO 1": "You can jump, you can shake. The lining in it is going to prevent it from slipping down. And on top of that, it covers a lot of that back fat, it's comfortable, and it comes with pads. This is the perfect strapless bra. When I tell y'all it don't move no matter what you do. So if you need a strapless bra, go ahead and click the link. Because you definitely can't go wrong by getting these kind of bras. And on top of that, it also comes with straps. So if you do want to hold it up for that extra security, you have some clear straps that you can clip onto it. And for size reference, I'm a 36D, so I got a large."}

    found = find_words_with_sentences(keywords, transcript["VIDEO 1"])
    for word, sentences in found.items():
        print(f"Keyword '{word}' found in:")
        for s in sentences:
            print(f"  - {s}")
