from advisor.ingest.pipeline import looks_binary


def test_raw_pdf_bytes_are_not_indexed():
    assert looks_binary("<6cbafb85089a606c31fdad86f4299aa4> ] >> startxref 193083 %%EOF")
    assert looks_binary("�" * 40 + " h_5/ yO F/")


def test_normal_text_is_kept():
    assert not looks_binary("Students need 75 percent attendance to sit the final examination.")
    assert not looks_binary("The endobject of the study plan")
    assert not looks_binary("")
