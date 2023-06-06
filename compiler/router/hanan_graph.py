# See LICENSE for licensing information.
#
# Copyright (c) 2016-2023 Regents of the University of California, Santa Cruz
# All rights reserved.
#
import heapq
from copy import deepcopy
from openram import debug
from openram.base.vector import vector
from openram.base.vector3d import vector3d
from openram.tech import drc
from .direction import direction
from .hanan_node import hanan_node
from .hanan_probe import hanan_probe


class hanan_graph:
    """ This is the Hanan graph created from the blockages. """

    def __init__(self, router):

        # This is the Hanan router that uses this graph
        self.router = router
        self.source_nodes = []
        self.target_nodes = []


    def inside_shape(self, point, shape):
        """ Return if the point is inside the shape. """

        # Check if they're on the same layer
        if point.z != self.router.get_zindex(shape.lpp):
            return False
        # Check if the point is inside the shape
        ll, ur = shape.rect
        return shape.on_segment(ll, point, ur)


    def is_probe_blocked(self, p1, p2):
        """
        Return if a probe sent from p1 to p2 encounters a blockage.
        The probe must be sent vertically or horizontally.
        This function assumes that p1 and p2 are on the same layer.
        """

        probe_shape = hanan_probe(p1, p2, self.router.vert_lpp if p1.z else self.router.horiz_lpp)
        # Check if any blockage blocks this probe
        for blockage in self.graph_blockages:
            # Check if two shapes overlap
            if blockage.overlaps(probe_shape):
                return True
        return False


    def create_graph(self, source, target):
        """ Create the Hanan graph to run routing on later. """
        debug.info(0, "Creating the Hanan graph for source '{0}' and target'{1}'.".format(source, target))

        self.source = source
        self.target = target

        # Find the region to be routed and only include objects inside that region
        region = deepcopy(source)
        region.bbox([target])
        region = region.inflated_pin(multiple=1)
        debug.info(0, "Routing region is {}".format(region.rect))

        # Find the blockages that are in the routing area
        self.graph_blockages = []
        for blockage in self.get_blockages(source.name):
            # Set the region's lpp to current blockage's lpp so that the
            # overlaps method works
            region.lpp = blockage.lpp
            if region.overlaps(blockage):
                self.graph_blockages.append(blockage)
        debug.info(0, "Number of blockages detected in the routing region: {}".format(len(self.graph_blockages)))

        # Create the Hanan graph
        x_values, y_values = self.generate_cartesian_values()
        self.generate_hanan_nodes(x_values, y_values)
        self.remove_blocked_nodes()
        debug.info(0, "Number of nodes in the routing graph: {}".format(len(self.nodes)))


    def get_blockages(self, pin_name):
        """
        Return all blockages for this routing region, including pins with
        different name.
        """

        # Create a copy of blockages
        blockages = self.router.blockages[:]
        # Create a copy of pins with different name than the routed pins
        offset = self.router.layer_widths[0] / 2
        for name, pins in self.router.pins.items():
            if name == pin_name:
                continue
            for pin in pins:
                blockages.append(deepcopy(pin).inflated_pin(spacing=offset, multiple=1))
        return blockages


    def generate_cartesian_values(self):
        """
        Generate x and y values from all the corners of the shapes in the
        routing region.
        """

        x_values = set()
        y_values = set()

        # Add the source and target values
        for shape in [self.source, self.target]:
            aspect_ratio = shape.width() / shape.height()
            # If the pin is tall or fat, add two points on the ends
            if aspect_ratio <= 0.5: # Tall pin
                points = [shape.uc(), shape.bc()]
            elif aspect_ratio >= 2: # Fat pin
                points = [shape.lc(), shape.rc()]
            else: # Square-like pin
                points = [shape.center()]
            for p in points:
                x_values.add(p.x)
                y_values.add(p.y)

        # Add corners for blockages
        offset = drc["grid"]
        for blockage in self.graph_blockages:
            ll, ur = blockage.rect
            # Add minimum offset to the blockage corner nodes to prevent overlaps
            x_values.update([ll.x - offset, ur.x + offset])
            y_values.update([ll.y - offset, ur.y + offset])

        # Sort x and y values
        x_values = list(x_values)
        y_values = list(y_values)
        x_values.sort()
        y_values.sort()

        return x_values, y_values


    def generate_hanan_nodes(self, x_values, y_values):
        """
        Generate all Hanan nodes using the cartesian values and connect the
        orthogonal neighbors.
        """

        y_len = len(y_values)
        left_offset = -(y_len * 2)
        self.nodes = []
        for x in x_values:
            for y in y_values:
                below_node = hanan_node([x, y, 0])
                above_node = hanan_node([x, y, 1])

                # Connect these two neighbors
                below_node.add_neighbor(above_node)

                # Find potential orthogonal neighbor nodes
                belows = []
                aboves = []
                count = len(self.nodes) // 2
                if count % y_len: # Down
                    belows.append(-2)
                    aboves.append(-1)
                if count >= y_len: # Left
                    belows.append(left_offset)
                    aboves.append(left_offset + 1)

                # Add these connections if not blocked by a blockage
                for i in belows:
                    node = self.nodes[i]
                    if not self.is_probe_blocked(below_node.center, node.center):
                        below_node.add_neighbor(node)
                for i in aboves:
                    node = self.nodes[i]
                    if not self.is_probe_blocked(above_node.center, node.center):
                        above_node.add_neighbor(node)

                # Save source and target nodes
                for node in [below_node, above_node]:
                    if self.inside_shape(node.center, self.source):
                        self.source_nodes.append(node)
                    elif self.inside_shape(node.center, self.target):
                        self.target_nodes.append(node)

                self.nodes.append(below_node)
                self.nodes.append(above_node)


    def remove_blocked_nodes(self):
        """ Remove the Hanan nodes that are blocked by a blockage. """

        for i in range(len(self.nodes) - 1, -1, -1):
            node = self.nodes[i]
            point = node.center
            for blockage in self.graph_blockages:
                # Remove if the node is inside a blockage
                if self.inside_shape(point, blockage):
                    node.remove_all_neighbors()
                    self.nodes.remove(node)
                    break


    def find_shortest_path(self):
        """
        Find the shortest path from the source node to target node using the
        A* algorithm.
        """

        # Heuristic function to calculate the scores
        def h(node):
            """ Return the estimated distance to the closest target. """
            min_dist = float("inf")
            for t in self.target_nodes:
                dist = t.center.distance(node.center) + abs(t.center.z - node.center.z)
                if dist < min_dist:
                    min_dist = dist
            return min_dist

        # Initialize data structures to be used for A* search
        queue = []
        close_set = set()
        came_from = {}
        g_scores = {}
        f_scores = {}

        # Initialize score values for the source nodes
        for node in self.source_nodes:
            g_scores[node.id] = 0
            f_scores[node.id] = h(node)
            heapq.heappush(queue, (f_scores[node.id], node.id, node))

        # Run the A* algorithm
        while len(queue) > 0:
            # Get the closest node from the queue
            current = heapq.heappop(queue)[2]

            # Skip this node if already discovered
            if current in close_set:
                continue
            close_set.add(current)

            # Check if we've reached the target
            if current in self.target_nodes:
                path = []
                while current.id in came_from:
                    path.append(current)
                    current = came_from[current.id]
                path.append(current)
                return path

            # Update neighbor scores
            for node in current.neighbors:
                tentative_score = current.get_edge_cost(node) + g_scores[current.id]
                if node.id not in g_scores or tentative_score < g_scores[node.id]:
                    came_from[node.id] = current
                    g_scores[node.id] = tentative_score
                    f_scores[node.id] = tentative_score + h(node)
                    heapq.heappush(queue, (f_scores[node.id], node.id, node))

        # Return None if not connected
        return None