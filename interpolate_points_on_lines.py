"""
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*   Created by: Stephan "preinzi" Preinstorfer (LiberGIS)                 *
*   Last update: 23.07.2026                                               *
*   SPDX-License-Identifier: GPL-2.0-or-later                             *
*                                                                         *
***************************************************************************
"""

import bisect
import math

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsProcessingException,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterFeatureSink,
                       QgsFeature,
                       QgsFeatureSink,
                       QgsField,
                       QgsFields,
                       QgsGeometry,
                       QgsPoint,
                       QgsLineString,
                       QgsWkbTypes)


def _numeric_key(value):
    """
    Normalizes an ID value for matching purposes so that, e.g., an Int
    field and a Double field carrying the same underlying ID still match
    as the same line. Falls back to the raw value if it isn't numeric.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


class CreateMValueLinesAlgorithm(QgsProcessingAlgorithm):
    """
    This algorithm creates LineStringZM geometries that keep the exact shape
    of the input lines. Each measurement point is projected (snapped) onto
    its line and inserted as an extra vertex at the correct position along
    the line. Z-values at the measurement points come from the chosen point
    field; Z-values at the line's own vertices are linearly interpolated
    between the surrounding measurement points. M-values represent the
    station (distance along the line) everywhere.

    A second layer is also produced: points spaced at a regular
    interval along each output line, carrying Z and M both in their
    geometry and as regular attribute fields. Where an input measurement
    point falls within half an interval of a regularly spaced position,
    that measurement point is used instead of generating a new point
    there.

    A third output layer represents the main result of this tool. The lines
    are segmented at the location of the previously generated points in
    order to create individual line geometries, which inherit the attributes
    of the points.

    All Processing algorithms should extend the QgsProcessingAlgorithm
    class.

    Most of this script was generated with claude.ai.
    """

    # Constants used to refer to parameters and outputs.

    LINE_LAYER = 'LINE_LAYER'
    MEASUREMENT_POINTS = 'MEASUREMENT_POINTS'
    LINE_ID_FIELD = 'LINE_ID_FIELD'
    POINT_ID_FIELD = 'POINT_ID_FIELD'
    Z_VALUE_FIELD = 'Z_VALUE_FIELD'
    TOLERANCE = 'TOLERANCE'
    INTERVAL = 'INTERVAL'
    OUTPUT = 'OUTPUT'
    POINT_OUTPUT = 'POINT_OUTPUT'
    SEGMENT_OUTPUT = 'SEGMENT_OUTPUT'

    def tr(self, string):
        """
        Returns a translatable string with the self.tr() function.
        """
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateMValueLinesAlgorithm()

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm.
        """
        return 'interpolate_Z_M_geometries'

    def displayName(self):
        """
        Returns the translated algorithm name (user visible).
        """
        return self.tr('Interpolate Points on Lines')

    def group(self):
        """
        Returns the name of the group this algorithm belongs to.
        """
        return self.tr('Vector Analysis')

    def groupId(self):
        """
        Returns the unique ID of the group this algorithm belongs to.
        """
        return 'vector_analysis'

    def helpUrl(self):
        """
        Returns a url pointing to a resource describing the algorithm.
        """
        # TODO: replace with your actual GitHub repo URL once published
        return 'https://github.com/preinzi/REPLACE-WITH-YOUR-REPO-NAME#readme'

    def shortHelpString(self):
        """
        Returns a localised short helper string for the algorithm.
        """
        return self.tr(
            'Creates LineStringZM geometries that follow the exact geometry of the '
            'input lines. Measurement points are snapped onto the line and inserted '
            'as extra vertices; Z-values at the measurement points come from the '
            'selected point field, Z-values at the line\'s own vertices are linearly '
            'interpolated between the surrounding measurement points, and M-values '
            'represent the station (distance along the line).\n\n'
            'A second output of regularly spaced points along each line can also '
            'produced. Where an input measurement point already falls within half '
            'an interval of a generated position, that measurement point is reused '
            'instead of creating a redundant nearby point.\n\n'
            'A third output layer represents the main result of this tool. The lines '
            'are segmented at the location of the previously generated points in '
            'order to create individual line geometries, which inherit the attributes '
            'of the points.\n\n'
            'If less than 2 input points are found for a line, this line will be '
            'skipped.\n\n'
            'Inputs:\n'
            '- Line layer: Line features with unique IDs\n'
            '- Measurement points: Point features with line ID and numeric values\n'
            '- Line ID field: Field containing unique IDs to identify the lines\n'
            '- Point ID field: Field containing IDs which math the points to the lines\n'
            '- Z-value field: Field (in points layer) containing measurement information\n'
            '- Tolerance: Maximum distance a point '
            'may sit from the line before a warning is raised\n'
            '- Point interval: Spacing between the '
            'generated points along the output line\n\n'
            'Outputs:\n'
            '- Output line segments: Line layer with one individual line feature '
            'per segment between two consecutive output points (measurement or '
            'generated), carrying the "station" / "measure" / "point_type" '
            'attributes of the segment\'s first point.\n'
            '- Output lines: LineStringZM layer that keeps the original line '
            'vertices plus the measurement points, with Z = measurement value '
            '(interpolated where needed) and M = distance along line.\n'
            '- Output points: Point layer with points every "point interval" map '
            'units along each output line (measurement points are reused where '
            'close enough instead of adding a new point), carrying Z and M in the '
            'geometry and duplicated in the "station" / "measure" attribute fields, '
            'plus the line ID and a point_type field (measurement vs generated).'
        )

    def initAlgorithm(self, config=None):
        """
        Here we define the inputs and output of the algorithm.
        """
        # We add the line layer (line vector features). Using a
        # FeatureSource parameter gives the algorithm dialog an
        # automatic "Selected features only" checkbox for this input.
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.LINE_LAYER,
                self.tr('Line layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        # We add the measurement points layer (point vector features).
        # FeatureSource also gives an "Selected features only" checkbox.
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.MEASUREMENT_POINTS,
                self.tr('Measurement points layer'),
                [QgsProcessing.TypeVectorPoint]
            )
        )

        # We add the line ID field from the line layer
        self.addParameter(
            QgsProcessingParameterField(
                self.LINE_ID_FIELD,
                self.tr('Line ID field'),
                parentLayerParameterName=self.LINE_LAYER,
                type=QgsProcessingParameterField.Any
            )
        )

        # We add the point ID field from the measurement points layer
        self.addParameter(
            QgsProcessingParameterField(
                self.POINT_ID_FIELD,
                self.tr('Point ID field'),
                parentLayerParameterName=self.MEASUREMENT_POINTS,
                type=QgsProcessingParameterField.Any
            )
        )

        # We add the Z-value field from the measurement points layer
        self.addParameter(
            QgsProcessingParameterField(
                self.Z_VALUE_FIELD,
                self.tr('Z-value field (numeric)'),
                parentLayerParameterName=self.MEASUREMENT_POINTS,
                type=QgsProcessingParameterField.Numeric
            )
        )

        # We add a tolerance parameter used to warn about points that are
        # not actually sitting on the line
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('Tolerance (map units) - warn if a point is farther from the line than this'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.1,
                minValue=0.0
            )
        )

        # We add the interval parameter used to space the generated points
        self.addParameter(
            QgsProcessingParameterNumber(
                self.INTERVAL,
                self.tr('Point interval (map units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0001
            )
        )

        # We add the output parameter for the line segments.
        segment_output_param = QgsProcessingParameterFeatureSink(
            self.SEGMENT_OUTPUT,
            self.tr('Output line segments'),
            QgsProcessing.TypeVectorLine,
            optional=True
        )
        segment_output_param.setCreateByDefault(True)
        self.addParameter(segment_output_param)

        # We add the output parameter for the lines. It's optional and not
        # created by default.
        line_output_param = QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            self.tr('Output simple lines'),
            QgsProcessing.TypeVectorLine,
            optional=True
        )
        line_output_param.setCreateByDefault(False)
        self.addParameter(line_output_param)

        # We add the output parameter for the generated points. It's also
        # optional and not created by default.
        point_output_param = QgsProcessingParameterFeatureSink(
            self.POINT_OUTPUT,
            self.tr('Output points'),
            QgsProcessing.TypeVectorPoint,
            optional=True
        )
        point_output_param.setCreateByDefault(False)
        self.addParameter(point_output_param)

    def processAlgorithm(self, parameters, context, feedback):
        """
        Here is where the processing itself takes place.
        """

        # Retrieve the input layers as feature sources - this respects the
        # "Selected features only" checkbox if the user ticked it
        line_layer = self.parameterAsSource(
            parameters,
            self.LINE_LAYER,
            context
        )

        points_layer = self.parameterAsSource(
            parameters,
            self.MEASUREMENT_POINTS,
            context
        )

        # Retrieve field names
        line_id_field = self.parameterAsString(
            parameters,
            self.LINE_ID_FIELD,
            context
        )

        point_id_field = self.parameterAsString(
            parameters,
            self.POINT_ID_FIELD,
            context
        )

        z_value_field = self.parameterAsString(
            parameters,
            self.Z_VALUE_FIELD,
            context
        )

        tolerance = self.parameterAsDouble(
            parameters,
            self.TOLERANCE,
            context
        )

        interval = self.parameterAsDouble(
            parameters,
            self.INTERVAL,
            context
        )

        # If layers were not found, throw an exception
        if line_layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.LINE_LAYER)
            )

        if points_layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.MEASUREMENT_POINTS)
            )

        if interval <= 0:
            raise QgsProcessingException(
                self.tr('Point interval must be greater than 0.')
            )

        # Send some information to the user
        feedback.pushInfo(
            self.tr('Processing CRS: {}').format(line_layer.sourceCrs().authid())
        )

        # Create fields object from the input line layer (for the line output)
        fields = line_layer.fields()

        # Create output sink for the lines. This output is optional and
        # skipped by default, so `sink` may legitimately be None if the
        # user hasn't enabled it - that's not an error condition.
        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.LineStringZM,  # Use ZM geometry type
            line_layer.sourceCrs()
        )

        # Build the fields for the point output: the line ID field (matching
        # its original type), plus regular attribute fields carrying the same
        # M and Z values that are also stored in the point geometry, plus a
        # flag saying whether the point is an original measurement or a
        # generated one.
        line_id_field_type = line_layer.fields().field(
            line_layer.fields().indexOf(line_id_field)
        ).type()

        point_fields = QgsFields()
        point_fields.append(QgsField(line_id_field, line_id_field_type))
        point_fields.append(QgsField('station', QVariant.Double))
        point_fields.append(QgsField('measure', QVariant.Double))
        point_fields.append(QgsField('point_type', QVariant.String))

        # Create output sink for the line segments. Uses the same field
        # schema as the points, since each segment carries the attributes
        # of its first point. This output is optional but created by
        # default, so `segment_sink` will normally not be None.
        (segment_sink, segment_dest_id) = self.parameterAsSink(
            parameters,
            self.SEGMENT_OUTPUT,
            context,
            point_fields,
            QgsWkbTypes.LineString,
            line_layer.sourceCrs()
        )

        # Create output sink for the points. Also optional and skipped by
        # default, so `point_sink` may legitimately be None.
        (point_sink, point_dest_id) = self.parameterAsSink(
            parameters,
            self.POINT_OUTPUT,
            context,
            point_fields,
            QgsWkbTypes.PointZM,
            line_layer.sourceCrs()
        )

        # Index measurement points by line ID. Points are keyed by a
        # normalized numeric form of the point ID field so that, e.g., a
        # Line ID field stored as Integer and a Point ID field stored as
        # Double still match correctly as long as both refer to the same
        # underlying numeric ID.
        points_by_line = {}
        for point_feature in points_layer.getFeatures():
            line_id_key = _numeric_key(point_feature[point_id_field])
            if line_id_key not in points_by_line:
                points_by_line[line_id_key] = []

            point_geom = point_feature.geometry().asPoint()
            points_by_line[line_id_key].append(
                {
                    "x": point_geom.x(),
                    "y": point_geom.y(),
                    "z_value": point_feature[z_value_field],
                }
            )

        # Compute the number of steps to display within the progress bar
        total = line_layer.featureCount()
        step_size = 100.0 / total if total > 0 else 0

        # Track how many lines actually found a matching set of measurement
        # points, so we can warn about a systematic ID mismatch afterwards.
        lines_matched = 0

        # Process each line
        for current, line_feature in enumerate(line_layer.getFeatures()):
            # Stop the algorithm if cancel button has been clicked
            if feedback.isCanceled():
                break

            line_id = line_feature[line_id_field]
            line_id_key = _numeric_key(line_id)

            # Get measurement points for this line
            if line_id_key not in points_by_line:
                feedback.pushWarning(
                    self.tr(
                        "Line '{}': No measurement points found. "
                        "Skipping line."
                    ).format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            lines_matched += 1

            if len(points_by_line[line_id_key]) < 2:
                feedback.pushWarning(
                    self.tr(
                        "Line '{}': Less than 2 measurement points found. "
                        "Minimum 2 points required. Skipping line."
                    ).format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            line_geom = line_feature.geometry()

            # Convert multipart to single part if needed
            if line_geom.isMultipart():
                parts = line_geom.asMultiPolyline()
                if len(parts) > 0:
                    if len(parts) > 1:
                        feedback.pushWarning(
                            self.tr(
                                "Line '{}': Multipart line has {} parts; only "
                                "the first part is used, the rest are ignored."
                            ).format(line_id, len(parts))
                        )
                    # Reconstruct as a single-part geometry
                    line_geom = QgsGeometry(QgsLineString(parts[0]))
                else:
                    feedback.pushWarning(
                        self.tr("Line '{}': Empty multipart geometry.").format(line_id)
                    )
                    feedback.setProgress(int((current + 1) * step_size))
                    continue

            # Keep the line's own shape: record every original vertex
            # together with its station (distance along the line). This is
            # computed by summing the distance between consecutive vertices
            # in their actual order, rather than via lineLocatePoint on each
            # vertex.
            shape_vertices = []
            cumulative_distance = 0.0
            previous_xy = None
            for vertex in line_geom.vertices():
                vx, vy = vertex.x(), vertex.y()
                if previous_xy is not None:
                    cumulative_distance += math.hypot(vx - previous_xy[0], vy - previous_xy[1])
                shape_vertices.append(
                    {"station": cumulative_distance, "x": vx, "y": vy, "is_measurement": False}
                )
                previous_xy = (vx, vy)

            # Snap each measurement point onto the line, and record it's
            # station. Points should already sit on the line, so we also
            # check the actual distance against the tolerance and warn if
            # it's exceeded (the point is still used, snapped to the line).
            measurement_vertices = []
            for point in points_by_line[line_id_key]:
                point_geom = QgsGeometry(QgsPoint(point["x"], point["y"]))
                station = line_geom.lineLocatePoint(point_geom)
                snapped_geom = line_geom.interpolate(station)

                if snapped_geom.isEmpty():
                    feedback.pushWarning(
                        self.tr(
                            "Line '{}': Could not project a measurement "
                            "point onto the line. Skipping point."
                        ).format(line_id)
                    )
                    continue

                snapped_point = snapped_geom.asPoint()

                distance_off_line = point_geom.distance(line_geom)
                if distance_off_line > tolerance:
                    feedback.pushWarning(
                        self.tr(
                            "Line '{}': a measurement point is "
                            "{:.4f} map units away from the "
                            "line (tolerance is {})."
                        ).format(line_id, distance_off_line, tolerance)
                    )

                measurement_vertices.append(
                    {
                        "station": station,
                        "x": snapped_point.x(),
                        "y": snapped_point.y(),
                        "is_measurement": True,
                        "z_value": point["z_value"],
                    }
                )

            if len(measurement_vertices) < 2:
                feedback.pushWarning(
                    self.tr(
                        "Line '{}': Less than 2 valid measurement points "
                        "after projecting onto the line. Skipping line."
                    ).format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            # Merge the line's own vertices with the measurement points,
            # sorted by station.
            all_vertices = shape_vertices + measurement_vertices
            all_vertices.sort(key=lambda v: v["station"])

            # Drop near-duplicate stations (e.g. a measurement point that
            # falls exactly on an existing line vertex), preferring the
            # measurement point's data so its Z-value is kept exactly.
            merged_vertices = []
            station_eps = 1e-8
            for v in all_vertices:
                if (merged_vertices
                        and abs(v["station"] - merged_vertices[-1]["station"]) <= station_eps):
                    if v["is_measurement"] and not merged_vertices[-1]["is_measurement"]:
                        merged_vertices[-1] = v
                    continue
                merged_vertices.append(v)

            # Interpolate Z linearly between measurement points for the
            # vertices that came from the line's own geometry. Vertices
            # before the first / after the last measurement point take that
            # point's value (flat extrapolation).
            measurement_stations = [v["station"] for v in merged_vertices if v["is_measurement"]]
            measurement_values = [v["z_value"] for v in merged_vertices if v["is_measurement"]]

            def interpolated_z(station):
                idx = bisect.bisect_left(measurement_stations, station)
                if idx <= 0:
                    return measurement_values[0]
                if idx >= len(measurement_stations):
                    return measurement_values[-1]
                s0, s1 = measurement_stations[idx - 1], measurement_stations[idx]
                v0, v1 = measurement_values[idx - 1], measurement_values[idx]
                if s1 == s0:
                    return v0
                t = (station - s0) / (s1 - s0)
                return v0 + t * (v1 - v0)

            # Create LineStringZM with Z-values from the selected field
            # (interpolated at the line's own vertices) and M-values as station
            points_zm = [
                QgsPoint(
                    v["x"],
                    v["y"],
                    z=v["z_value"] if v["is_measurement"] else interpolated_z(v["station"]),
                    m=v["station"],
                )
                for v in merged_vertices
            ]

            line_zm = QgsLineString(points_zm)

            # Write feature to the line output sink (if that output wasn't skipped)
            if sink is not None:
                feature = QgsFeature(fields)
                feature.setGeometry(line_zm)
                feature[line_id_field] = line_id
                sink.addFeature(feature, QgsFeatureSink.FastInsert)

            # Generate points at a regular interval along the line, used
            # for both the point output and the line segment output (if
            # either of them wasn't skipped). Where an input measurement
            # point already falls within half an interval of a regularly
            # spaced position, reuse that measurement point instead of
            # generating a new one there.
            if point_sink is not None or segment_sink is not None:
                total_length = merged_vertices[-1]["station"]

                grid_stations = []
                station = 0.0
                while station < total_length:
                    grid_stations.append(station)
                    station += interval
                grid_stations.append(total_length)  # always include the line's end

                def nearest_measurement_distance(s):
                    idx = bisect.bisect_left(measurement_stations, s)
                    candidates = []
                    if idx < len(measurement_stations):
                        candidates.append(abs(measurement_stations[idx] - s))
                    if idx > 0:
                        candidates.append(abs(measurement_stations[idx - 1] - s))
                    return min(candidates) if candidates else float('inf')

                output_points = []

                # Always output the real measurement points
                for v in merged_vertices:
                    if v["is_measurement"]:
                        output_points.append(
                            {
                                "station": v["station"],
                                "x": v["x"],
                                "y": v["y"],
                                "z_value": v["z_value"],
                                "point_type": "measurement",
                            }
                        )

                # Add generated grid points, skipping ones too close to a
                # measurement point (the measurement point is used instead).
                # "Too close" means within half an interval, so a generated
                # point a full interval away from a measurement point is kept
                # rather than leaving a gap of two intervals.
                for s in grid_stations:
                    if nearest_measurement_distance(s) <= interval / 2:
                        continue

                    grid_point_geom = line_geom.interpolate(s)
                    if grid_point_geom.isEmpty():
                        continue
                    grid_point = grid_point_geom.asPoint()

                    output_points.append(
                        {
                            "station": s,
                            "x": grid_point.x(),
                            "y": grid_point.y(),
                            "z_value": interpolated_z(s),
                            "point_type": "generated",
                        }
                    )

                output_points.sort(key=lambda p: p["station"])

                if point_sink is not None:
                    for p in output_points:
                        point_feature = QgsFeature(point_fields)
                        point_feature.setGeometry(
                            QgsGeometry(QgsPoint(p["x"], p["y"], z=p["z_value"], m=p["station"]))
                        )
                        point_feature[line_id_field] = line_id
                        point_feature['station'] = p["station"]
                        point_feature['measure'] = p["z_value"]
                        point_feature['point_type'] = p["point_type"]
                        point_sink.addFeature(point_feature, QgsFeatureSink.FastInsert)

                # Build one individual line feature per segment between
                # two consecutive output points, carrying the attributes of
                # the segment's first (start) point.
                if segment_sink is not None:
                    for p_start, p_end in zip(output_points[:-1], output_points[1:]):
                        segment_geom = QgsGeometry(
                            QgsLineString([
                                QgsPoint(p_start["x"], p_start["y"]),
                                QgsPoint(p_end["x"], p_end["y"]),
                            ])
                        )

                        segment_feature = QgsFeature(point_fields)
                        segment_feature.setGeometry(segment_geom)
                        segment_feature[line_id_field] = line_id
                        segment_feature['station'] = p_start["station"]
                        segment_feature['measure'] = p_start["z_value"]
                        segment_feature['point_type'] = p_start["point_type"]
                        segment_sink.addFeature(segment_feature, QgsFeatureSink.FastInsert)

            # Update the progress bar
            feedback.setProgress(int((current + 1) * step_size))

        # If not a single line matched any measurement points, but both
        # layers actually have features, this is almost always an ID
        # field mismatch (e.g. text vs. numeric formatting) rather than a
        # genuine data gap - flag it explicitly so it's not mistaken for
        # a silent no-op.
        if total > 0 and points_layer.featureCount() > 0 and lines_matched == 0:
            feedback.pushWarning(
                self.tr(
                    'No line matched any measurement points at all. This '
                    'usually means the Line ID field and Point ID field do '
                    'not actually refer to the same IDs - double-check both '
                    'fields and their values.'
                )
            )

        # Send completion message
        feedback.pushInfo(
            self.tr('Successfully created ZM value lines, points and line segments.')
        )

        # Return the results of the algorithm
        return {
            self.OUTPUT: dest_id,
            self.POINT_OUTPUT: point_dest_id,
            self.SEGMENT_OUTPUT: segment_dest_id,
        }