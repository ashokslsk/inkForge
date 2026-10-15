// Copyright (C) 2022 The Qt Company Ltd.
// SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only


#ifndef SBK_QTLABSSTYLEKIT_PYTHON_H
#define SBK_QTLABSSTYLEKIT_PYTHON_H

//workaround to access protected functions
#define protected public

#include <sbkpython.h>
#include <sbkmodule.h>
#include <sbkconverter.h>
// Module Includes
#include <pyside6_qtwidgets_python.h>
#include <pyside6_qtgui_python.h>
#include <pyside6_qtcore_python.h>

// Bound library includes

QT_BEGIN_NAMESPACE
class QStyleKitStyle;
QT_END_NAMESPACE

// Type indices
enum : int {
    SBK_QStyleKitStyle_IDX                                   = 0,
    SBK_QtLabsStyleKit_IDX_COUNT                             = 1,
};

// This variable stores all Python types exported by this module.
extern Shiboken::Module::TypeInitStruct *SbkPySide6_QtLabsStyleKitTypeStructs;

// This variable stores the Python module object exported by this module.
extern PyObject *SbkPySide6_QtLabsStyleKitModuleObject;

// This variable stores all type converters exported by this module.
extern SbkConverter **SbkPySide6_QtLabsStyleKitTypeConverters;

// Converter indices
enum [[deprecated]] : int {
    SBK_QTLABSSTYLEKIT_QLIST_INT_IDX                         = 0, // QList<int>
    SBK_QTLABSSTYLEKIT_QLIST_QVARIANT_IDX                    = 1, // QList<QVariant>
    SBK_QTLABSSTYLEKIT_QLIST_QSTRING_IDX                     = 2, // QList<QString>
    SBK_QTLABSSTYLEKIT_QMAP_QSTRING_QVARIANT_IDX             = 3, // QMap<QString,QVariant>
    SBK_QTLABSSTYLEKIT_CONVERTERS_IDX_COUNT                  = 4,
};

// Converter indices
enum : int {
    SBK_QtLabsStyleKit_QList_int_IDX                         = 0, // QList<int>
    SBK_QtLabsStyleKit_QList_QVariant_IDX                    = 1, // QList<QVariant>
    SBK_QtLabsStyleKit_QList_QString_IDX                     = 2, // QList<QString>
    SBK_QtLabsStyleKit_QMap_QString_QVariant_IDX             = 3, // QMap<QString,QVariant>
    SBK_QtLabsStyleKit_CONVERTERS_IDX_COUNT                  = 4,
};
// Macros for type check

QT_WARNING_PUSH
QT_WARNING_DISABLE_DEPRECATED
namespace Shiboken
{

// PyType functions, to get the PyObjectType for a type T
template<> inline PyTypeObject *SbkType< ::QStyleKitStyle >() { return Shiboken::Module::get(SbkPySide6_QtLabsStyleKitTypeStructs[SBK_QStyleKitStyle_IDX]); }

} // namespace Shiboken

QT_WARNING_POP
#endif // SBK_QTLABSSTYLEKIT_PYTHON_H

