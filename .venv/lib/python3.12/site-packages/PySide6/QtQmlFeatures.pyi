# Copyright (C) 2022 The Qt Company Ltd.
# SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
"""
This file contains the exact signatures for all functions in module
PySide6.QtQmlFeatures, except for defaults which are replaced by "...".
"""

# mypy: disable-error-code="override, overload-overlap"
# Module `PySide6.QtQmlFeatures`

import PySide6.QtQmlFeatures
import PySide6.QtQml

import typing


class Change:
    name                      = ...  # type: typing.Any
    new                       = ...  # type: typing.Any
    old                       = ...  # type: typing.Any
    owner                     = ...  # type: typing.Any

    def __init__(self, name: str, old: typing.Any, new: typing.Any, owner: typing.Any, /) -> None: ...

    def __repr__(self, /) -> str: ...


class auto_properties:
    def __init__(self, klass: type, /) -> None: ...


class computed:
    def __init__(self, /, *dep_names: str) -> None: ...

    def __call__(self, function: typing.Callable, /) -> typing.Callable: ...


class effect:
    def __init__(self, /, *property_names: str) -> None: ...

    def __call__(self, function: typing.Callable, /) -> typing.Callable: ...


class load_qml_component:
    def __init__(self, engine: PySide6.QtQml.QQmlEngine, /, source: str = ..., module: str = ..., type_name: str = ...) -> None: ...


class watch:
    def __init__(self, property_name: str, /) -> None: ...

    def __call__(self, function: typing.Callable, /) -> typing.Callable: ...


# eof
