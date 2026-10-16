from app.api.academic import list_policies


class _EmptyRows:
    def mappings(self):
        return []


class _CapturingDb:
    def __init__(self):
        self.params = None

    def execute(self, _statement, params):
        self.params = params
        return _EmptyRows()


def test_blank_policy_filters_are_normalized_to_no_filter():
    db = _CapturingDb()

    assert list_policies(category="  ", search="", db=db) == []
    assert db.params == {"category": None, "search": None, "pattern": None}


def test_policy_search_is_trimmed_before_building_pattern():
    db = _CapturingDb()

    assert list_policies(search="  attendance  ", db=db) == []
    assert db.params["search"] == "attendance"
    assert db.params["pattern"] == "%attendance%"
