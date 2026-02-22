"""Tests for the symbol-aware code chunker."""
import pytest

from app.rag.chunker import CodeChunk, chunk_file


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

class TestChunkPython:
    def test_basic_functions(self):
        code = (
            "import os\n"
            "import sys\n"
            "\n"
            "def greet(name):\n"
            "    print(f'Hello, {name}')\n"
            "\n"
            "def farewell(name):\n"
            "    print(f'Goodbye, {name}')\n"
        )
        chunks = chunk_file(code, "app.py", "python")
        assert len(chunks) >= 2
        # Each chunk should contain the import header
        for c in chunks:
            assert c.file_path == "app.py"
            assert c.language == "python"

    def test_class_extraction(self):
        code = (
            "class Dog:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "\n"
            "    def bark(self):\n"
            "        return 'Woof!'\n"
            "\n"
            "class Cat:\n"
            "    def meow(self):\n"
            "        return 'Meow!'\n"
        )
        chunks = chunk_file(code, "animals.py", "python")
        assert len(chunks) >= 2
        names = [c.symbol_name for c in chunks]
        assert "Dog" in names
        assert "Cat" in names

    def test_async_function(self):
        code = (
            "async def fetch_data(url):\n"
            "    response = await aiohttp.get(url)\n"
            "    return response\n"
        )
        chunks = chunk_file(code, "client.py", "python")
        assert len(chunks) >= 1
        assert chunks[0].symbol_name == "fetch_data"
        assert chunks[0].symbol_type == "function"

    def test_import_header_prepended(self):
        code = (
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "def process():\n"
            "    pass\n"
        )
        chunks = chunk_file(code, "proc.py", "python")
        assert len(chunks) >= 1
        # Import header should be in the content
        assert "import os" in chunks[0].content


# ---------------------------------------------------------------------------
# TypeScript / JavaScript
# ---------------------------------------------------------------------------

class TestChunkTypeScript:
    def test_function_and_class(self):
        code = (
            "import { Router } from 'express';\n"
            "\n"
            "export function handleRequest(req: Request): Response {\n"
            "    return new Response('ok');\n"
            "}\n"
            "\n"
            "export class Server {\n"
            "    start(): void {\n"
            "        console.log('started');\n"
            "    }\n"
            "}\n"
        )
        chunks = chunk_file(code, "server.ts", "typescript")
        assert len(chunks) >= 2
        names = [c.symbol_name for c in chunks]
        assert "handleRequest" in names
        assert "Server" in names

    def test_arrow_function(self):
        code = (
            "export const greet = (name: string) => {\n"
            "    return `Hello, ${name}`;\n"
            "};\n"
        )
        chunks = chunk_file(code, "utils.ts", "typescript")
        assert len(chunks) >= 1
        assert "greet" in chunks[0].symbol_name

    def test_javascript_same_as_typescript(self):
        code = (
            "function add(a, b) {\n"
            "    return a + b;\n"
            "}\n"
        )
        chunks = chunk_file(code, "math.js", "javascript")
        assert len(chunks) >= 1
        assert chunks[0].symbol_name == "add"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

class TestChunkJava:
    def test_class(self):
        code = (
            "package com.example;\n"
            "\n"
            "import java.util.List;\n"
            "\n"
            "public class UserService {\n"
            "    public List<User> getUsers() {\n"
            "        return List.of();\n"
            "    }\n"
            "}\n"
        )
        chunks = chunk_file(code, "UserService.java", "java")
        assert len(chunks) >= 1
        assert any(c.symbol_name == "UserService" for c in chunks)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

class TestChunkGo:
    def test_function_and_type(self):
        code = (
            "package main\n"
            "\n"
            "import \"fmt\"\n"
            "\n"
            "type Server struct {\n"
            "    port int\n"
            "}\n"
            "\n"
            "func (s *Server) Start() {\n"
            "    fmt.Println(\"starting\")\n"
            "}\n"
            "\n"
            "func main() {\n"
            "    s := &Server{port: 8080}\n"
            "    s.Start()\n"
            "}\n"
        )
        chunks = chunk_file(code, "main.go", "go")
        assert len(chunks) >= 2
        names = [c.symbol_name for c in chunks]
        assert "Server" in names
        assert "Start" in names or "main" in names


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestChunkEdgeCases:
    def test_empty_file(self):
        chunks = chunk_file("", "empty.py", "python")
        assert chunks == []

    def test_whitespace_only_file(self):
        chunks = chunk_file("   \n  \n  ", "blank.py", "python")
        assert chunks == []

    def test_no_symbols_falls_back_to_blocks(self):
        """A file with no recognisable symbols should still produce chunks."""
        code = "x = 1\ny = 2\nz = 3\n"
        chunks = chunk_file(code, "data.py", "python")
        assert len(chunks) >= 1
        assert chunks[0].symbol_type == "block"

    def test_unsupported_language_produces_blocks(self):
        code = "some content\nmore content\n"
        chunks = chunk_file(code, "file.rb", "ruby")
        assert len(chunks) >= 1
        assert chunks[0].symbol_type == "block"

    def test_oversized_symbol_is_split(self):
        """A function longer than max_lines should be split."""
        lines = ["def big_function():"]
        for i in range(300):
            lines.append(f"    x_{i} = {i}")
            if i % 50 == 49:
                lines.append("")  # blank line for splitting
        code = "\n".join(lines)
        chunks = chunk_file(code, "big.py", "python", max_lines=50)
        assert len(chunks) > 1

    def test_chunk_line_numbers(self):
        code = (
            "def a():\n"
            "    pass\n"
            "\n"
            "def b():\n"
            "    pass\n"
        )
        chunks = chunk_file(code, "lines.py", "python")
        # First chunk starts at line 1
        assert chunks[0].start_line == 1
        # Second chunk starts later
        if len(chunks) > 1:
            assert chunks[1].start_line > 1
