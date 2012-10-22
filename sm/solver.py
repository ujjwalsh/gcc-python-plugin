#   Copyright 2012 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2012 Red Hat, Inc.
#
#   This is free software: you can redistribute it and/or modify it
#   under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see
#   <http://www.gnu.org/licenses/>.

############################################################################
# Solver: what states are possible at each location?
############################################################################

import sys

import gcc

from gccutils import DotPrettyPrinter, invoke_dot
from gccutils.graph import Graph, Node, Edge, \
    ExitNode, SplitPhiNode, \
    CallToReturnSiteEdge, CallToStart, ExitToReturnSite
from gccutils.dot import to_html

from libcpychecker.absinterp import Location, get_locations

import sm.checker
import sm.parser

VARTYPES = (gcc.VarDecl, gcc.ParmDecl, )

class StateVar:
    def __init__(self, state):
        self.state = state

    def __repr__(self):
        return 'StateVar(%r)' % self.state

    def copy(self):
        return StateVar(self.state)

    def __eq__(self, other):
        if isinstance(other, StateVar):
            # Note that although two StateVars may have equal state, we
            # also care about identity.
            return self.state == other.state

    def __hash__(self):
        return hash(self.state)

class Shape:
    def __init__(self, ctxt):
        self.ctxt = ctxt

        # a dict mapping from vars -> StateVar instances
        self._dict = {}
        # initial state is empty, an eligible var that isn't listed is assumed
        # to have its own unique StateVar value in the default state

    def __hash__(self):
        result = 0
        for k, v in self._dict.iteritems():
            result ^= hash(k)
            result ^= hash(v)
        return result

    def __eq__(self, other):
        if isinstance(other, Shape):
            return self._dict == other._dict

    def __ne__(self, other):
        if not isinstance(other, Shape):
            return True
        return self._dict != other._dict

    def __str__(self):
        mapping = ', '.join(['%s:%s' % (var, statevar.state)
                          for var, statevar in self._dict.iteritems()])
        return '{%s}' % mapping

    def __repr__(self):
        return repr(self._dict)

    def _copy(self):
        clone = Shape(self.ctxt)
        # 1st pass: clone the ShapeVar instances:
        shapevars = {}
        for shapevar in self._dict.values():
            shapevars[id(shapevar)] = shapevar.copy()

        # 2nd pass: update the new dict to point at the new ShapeVar instances,
        # preserving the aliasing within this dict (but breaking the aliasing
        # between old and new copies of the dict, so that when we change the
        # new Shape's values, we aren't changing the old Shape's values:
        for var, shapevar in self._dict.iteritems():
            clone._dict[var] = shapevars[id(shapevar)]

        return clone, shapevars

    def var_has_state(self, gccvar):
        '''Does the given gccvar have a non-default state?'''
        return gccvar in self._dict

    def get_state(self, var):
        assert isinstance(var, VARTYPES)
        if var in self._dict:
            return self._dict[var].state
        return self.ctxt.get_default_state()

    def set_state(self, var, state):
        assert isinstance(var, VARTYPES)
        if var in self._dict:
            # update existing StateVar (so that the change can be seen by
            # aliases):
            self._dict[var].state = state
        else:
            sv = StateVar(state)
            self._dict[var] = sv

    def iter_aliases(self, statevar):
        for gccvar in sorted(self._dict.keys()):
            if self._dict[gccvar] is statevar:
                yield gccvar

    def _assign_var(self, dstvar, srcvar):
        # ctxt.debug('Shape.assign_var(%r, %r)' % (dst, src))
        if srcvar not in self._dict:
            # Set a state, so that we can track that dst is now aliased to src
            self.set_state(srcvar, self.ctxt.get_default_state())
        self._dict[dstvar] = self._dict[srcvar]
        shapevar = self._dict[dstvar]

    def _purge_locals(self, fun):
        vars_ = fun.local_decls + fun.decl.arguments
        for gccvar in self._dict.keys():
            # Purge gcc.VarDecl and gcc.ParmDecl instances:
            if gccvar in vars_:
                del self._dict[gccvar]

class ShapeChange:
    """
    Captures the aliasing changes that occur to a Shape along an ExplodedEdge
    Does not capture changes to the states within the StateVar instances
    """
    def __init__(self, srcshape):
        self.srcshape = srcshape
        self.dstshape, self._shapevars = srcshape._copy()

    def assign_var(self, dstvar, srcvar):
        if isinstance(dstvar, gcc.SsaName):
            dstvar = dstvar.var
        if isinstance(srcvar, gcc.SsaName):
            srcvar = srcvar.var
        self.dstshape._assign_var(dstvar, srcvar)

    def purge_locals(self, fun):
        self.dstshape._purge_locals(fun)

    def iter_leaks(self):
        dstshapevars = self.dstshape._dict.values()
        for srcshapevar in self.srcshape._dict.values():
            dstshapevar = self._shapevars[id(srcshapevar)]
            if dstshapevar not in dstshapevars:
                # the ShapeVar has leaked:
                for gccvar in self.srcshape.iter_aliases(srcshapevar):
                    yield gccvar

class ExplodedGraph(Graph):
    """
    A graph of (innernode, shape) pairs, where "innernode" refers to
    nodes in an underlying graph (e.g. a StmtGraph or a Supergraph)
    """
    def __init__(self, ctxt):
        Graph.__init__(self)

        self.ctxt = ctxt

        # Mapping from (innernode, shape) to ExplodedNode:
        self._nodedict = {}

        # Mapping from
        #   (srcexpnode, dstexpnode, match, shapechange) tuples, where
        #   pattern can be None
        # to ExplodedEdge
        self._edgedict = {}

        self.entrypoints = []

        # List of ExplodedNode still to be processed by solver
        self.worklist = []

    def _make_edge(self, srcexpnode, dstexpnode, inneredge, match, shapechange):
        return ExplodedEdge(srcexpnode, dstexpnode, inneredge, match, shapechange)

    def lazily_add_node(self, innernode, shape):
        from gccutils.graph import SupergraphNode
        assert isinstance(innernode, SupergraphNode)
        assert isinstance(shape, Shape)
        key = (innernode, shape)
        if key not in self._nodedict:
            expnode = self.add_node(ExplodedNode(innernode, shape))
            self._nodedict[key] = expnode
            self.worklist.append(expnode)
        return self._nodedict[key]

    def lazily_add_edge(self, srcexpnode, dstexpnode, inneredge, match, shapechange):
        if match:
            assert isinstance(match, sm.checker.Match)
        key = (srcexpnode, dstexpnode, match, shapechange)
        if key not in self._edgedict:
            expedge = self.add_edge(srcexpnode, dstexpnode, inneredge, match, shapechange)
            self._edgedict[key] = expedge
        expedge = self._edgedict[key]

        # Some patterns match on ExplodedEdges (based on the src state):
        for sc in self.ctxt._stateclauses:
            # Locate any rules that could apply, regardless of the current
            # state:
            for pr in sc.patternrulelist:
                for match in pr.pattern.iter_expedge_matches(expedge, self):
                    # Now see if the rules apply for this state:
                    stateful_gccvar = match.get_stateful_gccvar(self.ctxt)
                    srcstate = expedge.srcnode.shape.get_state(stateful_gccvar)
                    if srcstate in sc.statelist:
                        mctxt = MatchContext(match, self, srcexpnode, inneredge)
                        self.ctxt.log('got match in state %r of %r at %s: %s'
                            % (srcstate,
                               str(pr.pattern),
                               inneredge,
                               match))
                        for outcome in pr.outcomes:
                            self.ctxt.log('applying outcome to %s => %s'
                                          % (mctxt.get_stateful_gccvar(),
                                             outcome))
                            outcome.apply(mctxt)

    def get_shortest_path_to(self, dstexpnode):
        result = None
        for srcexpnode in self.entrypoints:
            path = self.get_shortest_path(srcexpnode, dstexpnode)
            if path:
                if result:
                    if len(path) < len(result):
                        result = path
                else:
                    result = path
        return result

class ExplodedNode(Node):
    def __init__(self, innernode, shape):
        Node.__init__(self)
        self.innernode = innernode
        self.shape = shape

    def __repr__(self):
        return 'ExplodedNode(%r, %r)' % (self.innernode, self.shape)

    def __str__(self):
        return 'ExplodedNode(%s, %s)' % (self.innernode, self.shape)

    def get_gcc_loc(self):
        return self.innernode.get_gcc_loc()

    def to_dot_label(self, ctxt):
        from gccutils.dot import Table, Tr, Td, Text, Br

        table = Table()
        tr = table.add_child(Tr())
        tr.add_child(Td([Text('SHAPE: %s' % str(self.shape))]))
        tr = table.add_child(Tr())
        innerhtml = self.innernode.to_dot_html(ctxt)
        if innerhtml:
            tr.add_child(Td([innerhtml]))
        else:
            tr.add_child(Td([Text(str(self.innernode))]))
        return '<font face="monospace">' + table.to_html() + '</font>\n'

    def get_subgraph(self, ctxt):
        if 0:
            # group by function:
            return self.innernode.get_subgraph(ctxt)
        else:
            # ungrouped:
            return None

    @property
    def function(self):
        return self.innernode.function

class ExplodedEdge(Edge):
    def __init__(self, srcexpnode, dstexpnode, inneredge, match, shapechange):
        Edge.__init__(self, srcexpnode, dstexpnode)
        self.inneredge = inneredge
        if match:
            assert isinstance(match, sm.checker.Match)
        self.match = match
        self.shapechange = shapechange

    def __repr__(self):
        return ('%s(srcnode=%r, dstnode=%r, inneredge=%r, match=%r, shapechange=%r)'
                % (self.__class__.__name__, self.srcnode, self.dstnode,
                   self.inneredge.__class__, self.match))

    def to_dot_label(self, ctxt):
        result = self.inneredge.to_dot_label(ctxt)
        if self.match:
            result += to_html(self.match.description(ctxt))
        if self.srcnode.shape != self.dstnode.shape:
            result += to_html(' %s -> %s' % (self.srcnode.shape, self.dstnode.shape))
        return result.strip()

    def to_dot_attrs(self, ctxt):
        return self.inneredge.to_dot_attrs(ctxt)

class MatchContext:
    """
    A match of a specific rule, to be supplied to Outcome.apply()
    """
    def __init__(self, match, expgraph, srcexpnode, inneredge):
        from sm.checker import Match
        from gccutils.graph import SupergraphEdge
        assert isinstance(match, Match)
        assert isinstance(expgraph, ExplodedGraph)
        assert isinstance(srcexpnode, ExplodedNode)
        assert isinstance(inneredge, SupergraphEdge)
        self.match = match
        self.expgraph = expgraph
        self.srcexpnode = srcexpnode
        self.inneredge = inneredge

    @property
    def srcshape(self):
        return self.srcexpnode.shape

    @property
    def dstnode(self):
        return self.inneredge.dstnode

    def get_stateful_gccvar(self):
        return self.match.get_stateful_gccvar(self.expgraph.ctxt)

def make_exploded_graph(ctxt, innergraph):
    expgraph = ExplodedGraph(ctxt)
    for entry in innergraph.get_entry_nodes():
        expnode = expgraph.lazily_add_node(entry, Shape(ctxt)) # initial shape
        expgraph.entrypoints.append(expnode)
    while expgraph.worklist:
        srcexpnode = expgraph.worklist.pop()
        srcnode = srcexpnode.innernode
        #assert isinstance(srcnode, Node)
        stmt = srcnode.get_stmt()
        for edge in srcnode.succs:
            dstnode = edge.dstnode
            ctxt.debug('  edge from: %s' % srcnode)
            ctxt.debug('         to: %s' % dstnode)
            srcshape = srcexpnode.shape

            # Handle interprocedural edges:
            if isinstance(edge, CallToReturnSiteEdge):
                # Ignore the intraprocedural edge for a function call:
                continue
            elif isinstance(edge, CallToStart):
                # Alias the parameters with the arguments as necessary, so
                # e.g. a function that free()s an arg has the caller's var
                # marked as free also:
                shapechange = ShapeChange(srcshape)
                assert isinstance(srcnode.stmt, gcc.GimpleCall)
                # ctxt.debug(srcnode.stmt)
                for param, arg  in zip(srcnode.stmt.fndecl.arguments, srcnode.stmt.args):
                    # FIXME: change fndecl.arguments to fndecl.parameters
                    if 0:
                        ctxt.debug('  param: %r' % param)
                        ctxt.debug('  arg: %r' % arg)
                    if ctxt.is_stateful_var(arg):
                        shapechange.assign_var(param, arg)
                # ctxt.debug('dstshape: %r' % dstshape)
                dstexpnode = expgraph.lazily_add_node(dstnode, shapechange.dstshape)
                expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                   edge, None, shapechange)
                continue
            elif isinstance(edge, ExitToReturnSite):
                shapechange = ShapeChange(srcshape)
                # Propagate state through the return value:
                # ctxt.debug('edge.calling_stmtnode: %s' % edge.calling_stmtnode)
                if edge.calling_stmtnode.stmt.lhs:
                    exitsupernode = edge.srcnode
                    assert isinstance(exitsupernode.innernode, ExitNode)
                    returnstmtnode = exitsupernode.innernode.returnnode
                    assert isinstance(returnstmtnode.stmt, gcc.GimpleReturn)
                    retval = returnstmtnode.stmt.retval
                    if ctxt.is_stateful_var(retval):
                        shapechange.assign_var(edge.calling_stmtnode.stmt.lhs, retval)
                # ...and purge all local state:
                shapechange.purge_locals(edge.srcnode.function)
                dstexpnode = expgraph.lazily_add_node(dstnode, shapechange.dstshape)
                expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                   edge, None, shapechange)
                continue

            # Handle simple assignments so that variables inherit state:
            if isinstance(stmt, gcc.GimpleAssign):
                if 0:
                    ctxt.debug('gcc.GimpleAssign: %s' % stmt)
                    ctxt.debug('stmt.lhs: %r' % stmt.lhs)
                    ctxt.debug('stmt.rhs: %r' % stmt.rhs)
                    ctxt.debug('stmt.exprcode: %r' % stmt.exprcode)
                if stmt.exprcode == gcc.VarDecl:
                    shapechange = ShapeChange(srcshape)
                    shapechange.assign_var(stmt.lhs, stmt.rhs[0])
                    dstexpnode = expgraph.lazily_add_node(dstnode, shapechange.dstshape)
                    expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                       edge, None, shapechange)
                    continue
                elif stmt.exprcode == gcc.ComponentRef:
                    # Field lookup
                    compref = stmt.rhs[0]
                    if 0:
                        ctxt.debug(compref.target)
                        ctxt.debug(compref.field)
                    # The LHS potentially inherits state from the compref
                    if srcshape.var_has_state(compref.target):
                        ctxt.log('%s inheriting state "%s" from "%s" via field "%s"'
                            % (stmt.lhs,
                               srcshape.get_state(compref.target),
                               compref.target,
                               compref.field))
                        shapechange = ShapeChange(srcshape)
                        # For now we alias the states
                        shapechange.assign_var(stmt.lhs, compref.target)
                        dstexpnode = expgraph.lazily_add_node(dstnode, shapechange.dstshape)
                        expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                           edge, None, shapechange)
                        continue
            elif isinstance(stmt, gcc.GimplePhi):
                if 0:
                    ctxt.debug('gcc.GimplePhi: %s' % stmt)
                    ctxt.debug('srcnode: %s' % srcnode)
                    ctxt.debug('srcnode: %r' % srcnode)
                    ctxt.debug('srcnode.innernode: %s' % srcnode.innernode)
                    ctxt.debug('srcnode.innernode: %r' % srcnode.innernode)
                assert isinstance(srcnode.innernode, SplitPhiNode)
                shapechange = ShapeChange(srcshape)
                shapechange.assign_var(stmt.lhs,
                                       srcnode.innernode.rhs) # FIXME: could be a constant
                dstexpnode = expgraph.lazily_add_node(dstnode, shapechange.dstshape)
                expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                   edge, None, shapechange)
                continue

            matches = []
            for sc in ctxt._stateclauses:
                # Locate any rules that could apply, regardless of the current
                # state:
                for pr in sc.patternrulelist:
                    # ctxt.debug('%r: %r' % (srcshape, pr))
                    # For now, skip interprocedural calls and the
                    # ENTRY/EXIT nodes:
                    if not stmt:
                        continue
                    # Now see if the rules apply for the current state:
                    ctxt.debug('considering pattern %s for stmt: %s' % (pr.pattern, stmt) )
                    ctxt.debug('considering pattern %r for stmt: %r' % (pr.pattern, stmt) )
                    for match in pr.pattern.iter_matches(stmt, edge, ctxt):
                        ctxt.debug('pr.pattern: %r' % pr.pattern)
                        ctxt.debug('match: %r' % match)
                        srcstate = srcshape.get_state(match.get_stateful_gccvar(ctxt))
                        ctxt.debug('srcstate: %r' % srcstate)
                        if srcstate in sc.statelist:
                            assert len(pr.outcomes) > 0
                            ctxt.log('got match in state %r of %r at %r: %s'
                                % (srcstate,
                                   str(pr.pattern),
                                   str(stmt),
                                   match))
                            mctxt = MatchContext(match, expgraph, srcexpnode, edge)
                            for outcome in pr.outcomes:
                                ctxt.log('applying outcome to %s => %s'
                                         % (mctxt.get_stateful_gccvar(),
                                            outcome))
                                outcome.apply(mctxt)
                            matches.append(pr)
            if not matches:
                dstexpnode = expgraph.lazily_add_node(dstnode, srcshape)
                expedge = expgraph.lazily_add_edge(srcexpnode, dstexpnode,
                                                   edge, None, None)
    return expgraph

class Error:
    # A stored error
    def __init__(self, expnode, match, msg):
        self.expnode = expnode
        self.match = match
        self.msg = msg

    @property
    def gccloc(self):
        gccloc = self.expnode.innernode.get_gcc_loc()
        if gccloc is None:
            gccloc = self.function.end
        return gccloc

    @property
    def function(self):
        return self.expnode.function

    def __lt__(self, other):
        # Provide a sort order, so that they sort into source order
        return self.gccloc < other.gccloc

    def emit(self, ctxt, expgraph):
        """
        Display the error
        """
        from gccutils import error, inform
        loc = self.gccloc
        error(loc, self.msg)
        path = expgraph.get_shortest_path_to(self.expnode)
        # ctxt.debug('path: %r' % path)
        for expedge in path:
            # ctxt.debug(expnode)
            # FIXME: this needs to respect the StateVar, in case of a returned value...
            # we need to track changes to the value of the specific StateVar (but we can't, because it's a copy each time.... grrr...)
            # we should also report relevant aliasing information
            # ("foo" passed to fn bar as "baz"; "baz" returned from fn bar into "foo")
            # TODO: backtrace down the path, tracking the StateVar aliases of interest...

            stateful_gccvar = self.match.get_stateful_gccvar(ctxt)
            srcstate = expedge.srcnode.shape.get_state(stateful_gccvar)
            dststate = expedge.dstnode.shape.get_state(stateful_gccvar)
            # (they will always be equal for ssanames, so we have to work on
            # the underlying vars)
            if srcstate != dststate:
                gccloc = expedge.srcnode.innernode.get_gcc_loc()
                if gccloc:
                    if 1:
                        # Describe state change:
                        if expedge.match:
                            desc = expedge.match.description(ctxt)
                        else:
                            continue
                    else:
                        # Debugging information on state change:
                        desc = ('%s: %s -> %s'
                               % (ctxt.sm.name, srcstate, dststate))
                    inform(gccloc, desc)

        # repeat the message at the end of the path:
        if len(path) > 1:
            gccloc = path[-1].dstnode.innernode.get_gcc_loc()
            if gccloc:
                inform(gccloc, self.msg)

class Context:
    # An sm.checker.Sm (do we need any other context?)

    # in context, with a mapping from its vars to gcc.VarDecl
    # (or ParmDecl) instances
    def __init__(self, ch, sm, options):
        self.options = options

        self.ch = ch
        self.sm = sm

        # The Context caches some information about the sm to help
        # process it efficiently:
        #
        #   all state names:
        self.statenames = list(sm.iter_states())

        #   a mapping from str (decl names) to Decl instances
        self._decls = {}

        #   the stateful decl, if any:
        self._stateful_decl = None

        #   a mapping from str (pattern names) to NamedPattern instances
        self._namedpatterns = {}

        #   all StateClause instance, in order:
        self._stateclauses = []

        reachable_states = set([self.statenames[0]])

        # Set up the above attributes:
        from sm.checker import Decl, NamedPattern, StateClause, PythonFragment
        for clause in sm.clauses:
            if isinstance(clause, Decl):
                self._decls[clause.name] = clause
                if clause.has_state:
                    self._stateful_decl = clause
            elif isinstance(clause, NamedPattern):
                self._namedpatterns[clause.name] = clause
            elif isinstance(clause, StateClause):
                self._stateclauses.append(clause)
                for pr in clause.patternrulelist:
                    for outcome in pr.outcomes:
                        for state in outcome.iter_reachable_states():
                            reachable_states.add(state)

        # 2nd pass: validate the sm:
        for clause in sm.clauses:
            if isinstance(clause, StateClause):
                for state in clause.statelist:
                    if state not in reachable_states:
                        class UnreachableState(Exception):
                            def __init__(self, state):
                                self.state = state
                            def __str__(self):
                                return str(self.state)
                        raise UnreachableState(state)

        # Store the errors so that we can play them back in source order
        # (for greater predicability of selftests):
        self._errors = []

        # Run any initial python code:
        self.python_locals = {}
        self.python_globals = {}
        for clause in sm.clauses:
            if isinstance(clause, PythonFragment):
                filename = self.ch.filename
                if not filename:
                    filename = '<string>'
                expr = clause.get_source()
                code = compile(expr, filename, 'exec')
                # FIXME: the filename of the .sm file is correct, but the line
                # numbers will be wrong
                result = eval(code, self.python_globals, self.python_locals)

    def __repr__(self):
        return 'Context(%r)' % (self.statenames, )

    def _get_indent(self):
        # Indent by the stack depth:
        depth = 0
        f = sys._getframe()
        while f:
            depth += 1
            f = f.f_back
        return ' ' * depth

    def log(self, msg):
        # High-level logging
        if 0:
            sys.stderr.write('LOG  : %s: %s%s\n'
                             % (self.sm.name, self._get_indent(), msg))

    def debug(self, msg):
        # Lower-level logging
        if 0:
            sys.stderr.write('DEBUG: %s: %s%s\n'
                             % (self.sm.name, self._get_indent(), msg))

    def lookup_decl(self, declname):
        class UnknownDecl(Exception):
            def __init__(self, declname):
                self.declname = declname
            def __str__(self):
                return repr(declname)
        if declname not in self._decls:
            raise UnknownDecl(declname)
        return self._decls[declname]

    def lookup_pattern(self, patname):
        '''Lookup a named pattern'''
        class UnknownNamedPattern(Exception):
            def __init__(self, patname):
                self.patname = patname
            def __str__(self):
                return repr(patname)
        if patname not in self._namedpatterns:
            raise UnknownNamedPattern(patname)
        return self._namedpatterns[patname]

    def add_error(self, expgraph, expnode, match, msg):
        self.log('add_error(%r, %r, %r, %r)' % (expgraph, expnode, match, msg))
        err = Error(expnode, match, msg)
        if self.options.cache_errors:
            self._errors.append(err)
        else:
            # Easier to debug tracebacks this way:
            err.emit(self, expgraph)

    def emit_errors(self, expgraph):
        curfun = None
        curfile = None
        for error in self._errors:
            gccloc = error.gccloc
            if error.function != curfun or gccloc.file != curfile:
                # Fake the function-based output
                # e.g.:
                #    "tests/sm/examples/malloc-checker/input.c: In function 'use_after_free':"
                import sys
                sys.stderr.write("%s: In function '%s':\n"
                                 % (gccloc.file, error.function.decl.name))
                curfun = error.function
                curfile = gccloc.file
            error.emit(self, expgraph)

    def compare(self, gccexpr, smexpr):
        if 0:
            self.debug('  compare(%r, %r)' % (gccexpr, smexpr))

        if isinstance(gccexpr, (gcc.VarDecl, gcc.ParmDecl, gcc.SsaName)):
            #if gccexpr == self.var:
            # self.debug '%r' % self.sm.varclauses.name
            #if smexpr == self.sm.varclauses.name:
            decl = self.lookup_decl(smexpr)
            if decl.matched_by(gccexpr):
                return gccexpr

        if isinstance(gccexpr, gcc.IntegerCst):
            if isinstance(smexpr, (int, long)):
                if gccexpr.constant == smexpr:
                    return gccexpr
            if isinstance(smexpr, str):
                decl = self.lookup_decl(smexpr)
                if decl.matched_by(gccexpr):
                    return gccexpr

        if isinstance(gccexpr, gcc.AddrExpr):
            # Dereference:
            return self.compare(gccexpr.operand, smexpr)

        if isinstance(gccexpr, gcc.ComponentRef):
            # Dereference:
            return self.compare(gccexpr.target, smexpr)

        return None

    def get_default_state(self):
        return self.statenames[0]

    def is_stateful_var(self, gccexpr):
        '''
        Is this gcc.Tree of a kind that has state according to the current sm?
        '''
        if isinstance(gccexpr, gcc.SsaName):
            if isinstance(gccexpr.type, gcc.PointerType):
                # TODO: the sm may impose further constraints
                return True

def solve(ctxt, graph, name):
    ctxt.log('running %s' % ctxt.sm.name)
    ctxt.log('len(graph.nodes): %i' % len(graph.nodes))
    ctxt.log('len(graph.edges): %i' % len(graph.edges))
    ctxt.log('making exploded graph')
    expgraph = make_exploded_graph(ctxt, graph)
    ctxt.log('len(expgraph.nodes): %i' % len(expgraph.nodes))
    ctxt.log('len(expgraph.edges): %i' % len(expgraph.edges))

    if 0:
        # Debug: view the exploded graph:
        dot = expgraph.to_dot(name, ctxt)
        # ctxt.debug(dot)
        invoke_dot(dot, name)

    # Now report the errors, grouped by function, and in source order:
    ctxt._errors.sort()

    ctxt.emit_errors(expgraph)
