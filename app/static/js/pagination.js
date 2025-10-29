'use strict';

(function (global, $) {
    if (!$) {
        console.warn('Ownfoil Pagination requires jQuery.');
        return;
    }

    const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
    const toPositiveInteger = (value, fallback) => {
        const num = Number(value);
        if (Number.isFinite(num) && num > 0) {
            return Math.floor(num);
        }
        return fallback;
    };

    const ns = global.Ownfoil = global.Ownfoil || {};

    ns.Pagination = {
        create(options = {}) {
            const {
                container,
                getCurrentPage = () => 1,
                setCurrentPage = () => {},
                getItemsPerPage = () => 1,
                onPageChange = () => {},
                maxVisiblePages = 5,
                labels = {}
            } = options;

            const $container = typeof container === 'string' ? $(container) : $(container);
            if (!$container || !$container.length) {
                console.warn('Ownfoil Pagination: container not found.', container);
                return { update: () => {} };
            }

            const resolvedLabels = {
                first: labels.first || 'First page',
                previous: labels.previous || 'Previous page',
                next: labels.next || 'Next page',
                last: labels.last || 'Last page',
                go: labels.go || 'Go',
                goToPage: labels.goToPage || 'Go to page'
            };

            const maxVisible = Math.max(1, toPositiveInteger(maxVisiblePages, 5));

            return {
                update(nbDisplayedGames) {
                    const itemsPerPage = toPositiveInteger(getItemsPerPage(), 1);
                    const totalPages = Math.max(1, Math.ceil(nbDisplayedGames / itemsPerPage));
                    const hasResults = nbDisplayedGames > 0;

                    const originalPage = toPositiveInteger(getCurrentPage(), 1);
                    let currentPage = clamp(originalPage, 1, totalPages);

                    if (currentPage !== originalPage) {
                        setCurrentPage(currentPage);
                    }

                    const goToPage = (page) => {
                        const nextPage = clamp(page, 1, totalPages);
                        if (nextPage === currentPage) return;
                        currentPage = nextPage;
                        setCurrentPage(nextPage);
                        onPageChange(nextPage);
                    };

                    const appendControlButton = (targetPage, html, ariaLabel, disabled) => {
                        const item = $('<li class="page-item"></li>');
                        if (disabled) item.addClass('disabled');

                        const link = $(`<a class="page-link" href="#">${html}</a>`);
                        if (ariaLabel) link.attr('aria-label', ariaLabel);
                        link.on('click', (event) => {
                            event.preventDefault();
                            if (disabled) return;
                            goToPage(targetPage);
                        });
                        item.append(link);
                        $container.append(item);
                    };

                    const appendPageNumber = (page) => {
                        const isActive = page === currentPage;
                        const item = $('<li class="page-item"></li>');
                        if (isActive) item.addClass('active');

                        const link = $(`<a class="page-link" href="#">${page}</a>`);
                        if (isActive) link.attr('aria-current', 'page');
                        link.on('click', (event) => {
                            event.preventDefault();
                            if (isActive) return;
                            goToPage(page);
                        });
                        item.append(link);
                        $container.append(item);
                    };

                    const appendEllipsis = () => {
                        const item = $('<li class="page-item disabled page-ellipsis"></li>');
                        item.append('<span class="page-link">&hellip;</span>');
                        $container.append(item);
                    };

                    $container.empty();

                    appendControlButton(1, '<span aria-hidden="true">&laquo;</span>', resolvedLabels.first, currentPage === 1 || !hasResults);
                    appendControlButton(currentPage - 1, '<span aria-hidden="true">&lsaquo;</span>', resolvedLabels.previous, currentPage === 1 || !hasResults);

                    if (totalPages <= maxVisible + 2) {
                        for (let page = 1; page <= totalPages; page++) {
                            appendPageNumber(page);
                        }
                    } else {
                        appendPageNumber(1);

                        const halfWindow = Math.floor(maxVisible / 2);
                        let start = Math.max(2, currentPage - halfWindow);
                        let end = Math.min(totalPages - 1, currentPage + halfWindow);

                        if (currentPage <= halfWindow + 1) {
                            start = 2;
                            end = start + maxVisible - 1;
                        } else if (currentPage >= totalPages - halfWindow) {
                            end = totalPages - 1;
                            start = end - maxVisible + 1;
                        }

                        start = Math.max(2, start);
                        end = Math.min(totalPages - 1, end);

                        if (start > 2) appendEllipsis();

                        for (let page = start; page <= end; page++) {
                            appendPageNumber(page);
                        }

                        if (end < totalPages - 1) appendEllipsis();

                        appendPageNumber(totalPages);
                    }

                    appendControlButton(currentPage + 1, '<span aria-hidden="true">&rsaquo;</span>', resolvedLabels.next, currentPage === totalPages || !hasResults);
                    appendControlButton(totalPages, '<span aria-hidden="true">&raquo;</span>', resolvedLabels.last, currentPage === totalPages || !hasResults);

                    if (hasResults && totalPages > 1) {
                        const jumpItem = $('<li class="page-item page-jump ms-2"></li>');
                        const jumpGroup = $(`
                            <div class="input-group input-group-sm page-jump-group">
                                <input type="number" class="form-control" min="1" max="${totalPages}" value="${currentPage}" aria-label="${resolvedLabels.goToPage}">
                                <button class="btn btn-outline-secondary" type="button" aria-label="${resolvedLabels.goToPage}">${resolvedLabels.go}</button>
                            </div>
                        `);

                        const jumpInput = jumpGroup.find('input');
                        const jumpButton = jumpGroup.find('button');
                        const commitJump = () => {
                            const rawValue = parseInt(jumpInput.val(), 10);
                            if (!Number.isFinite(rawValue)) return;
                            goToPage(rawValue);
                        };

                        jumpButton.on('click', (event) => {
                            event.preventDefault();
                            commitJump();
                        });
                        jumpInput.on('keydown', (event) => {
                            if (event.key === 'Enter') {
                                event.preventDefault();
                                commitJump();
                            }
                        });

                        jumpItem.append(jumpGroup);
                        $container.append(jumpItem);
                    }
                }
            };
        }
    };
})(window, window.jQuery);
