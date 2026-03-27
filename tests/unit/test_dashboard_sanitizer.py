from execution_api.dashboard_sanitizer import sanitize_dashboard_code


class TestRewriteDependsToBind:
    def test_rewrites_simple_depends(self):
        code = (
            "widget = pn.widgets.Select(name='X', options=['A'])\n"
            "@pn.depends(widget)\n"
            "def my_plot(**kwargs):\n"
            "    return df.hvplot()\n"
            "app = pn.Column(widget, my_plot)\n"
        )
        result = sanitize_dashboard_code(code)
        assert "@pn.depends" not in result
        assert "pn.bind(my_plot" in result
        assert "def my_plot(**kwargs):" in result

    def test_rewrites_multiple_depends(self):
        code = (
            "w1 = pn.widgets.Select(name='A', options=['X'])\n"
            "w2 = pn.widgets.Select(name='B', options=['Y'])\n"
            "@pn.depends(w1, w2)\n"
            "def plot_a(**kwargs):\n"
            "    return 'a'\n"
            "@pn.depends(w1, w2)\n"
            "def plot_b(**kwargs):\n"
            "    return 'b'\n"
            "app = pn.Column(w1, w2, plot_a, plot_b)\n"
        )
        result = sanitize_dashboard_code(code)
        assert "@pn.depends" not in result
        assert "pn.bind(plot_a" in result
        assert "pn.bind(plot_b" in result

    def test_no_rewrite_without_depends(self):
        code = "def my_func():\n    return 42\napp = pn.Column(my_func)\n"
        result = sanitize_dashboard_code(code)
        assert "pn.bind" not in result


class TestEnsureServable:
    def test_adds_servable_when_missing(self):
        code = "app = pn.Column(a, b)\n"
        result = sanitize_dashboard_code(code)
        assert ".servable()" in result

    def test_no_duplicate_if_servable_exists(self):
        code = "app = pn.Column(a, b)\napp.servable()\n"
        result = sanitize_dashboard_code(code)
        assert result.count(".servable()") == 1

    def test_finds_last_layout_variable(self):
        code = "sidebar = pn.Column(a)\nmain = pn.Row(sidebar, b)\n"
        result = sanitize_dashboard_code(code)
        assert "main.servable()" in result

    def test_preserves_servable_with_title(self):
        code = 'app = pn.Column(a)\napp.servable(title="My Dashboard")\n'
        result = sanitize_dashboard_code(code)
        assert 'servable(title="My Dashboard")' in result

    def test_works_with_tabs(self):
        code = "dashboard = pn.Tabs(('A', a), ('B', b))\n"
        result = sanitize_dashboard_code(code)
        assert "dashboard.servable()" in result


class TestPassthroughStandardCode:
    def test_servable_and_template_preserved(self):
        code = (
            "import panel as pn\n"
            "pn.extension('tabulator', template='material')\n"
            "app = pn.Column(pn.pane.Markdown('# Hello'))\n"
            "app.servable()\n"
        )
        result = sanitize_dashboard_code(code)
        assert "template='material'" in result
        assert "app.servable()" in result
        assert result.count(".servable()") == 1

    def test_pn_serve_preserved(self):
        code = "app = pn.Column(a)\npn.serve(app)\n"
        result = sanitize_dashboard_code(code)
        assert "pn.serve(app)" in result

    def test_depends_rewrite_plus_servable(self):
        code = (
            "import panel as pn\n"
            "widget = pn.widgets.Select(name='Col', options=['A', 'B'])\n"
            "@pn.depends(widget)\n"
            "def my_plot(**kwargs):\n"
            "    return 'plot'\n"
            "layout = pn.Column(widget, my_plot)\n"
        )
        result = sanitize_dashboard_code(code)
        assert "@pn.depends" not in result
        assert "pn.bind(my_plot" in result
        assert ".servable()" in result
